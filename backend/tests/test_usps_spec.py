"""PLAN-012f-c D14: ``finance.usps`` against the USPS Addresses API's OpenAPI file.

The file the owner supplied is committed at ``docs/integrations/usps/addresses-v3r2_0.yaml``
(Addresses 3.0, ``info.version`` 3.3.1). These tests read it and pin what the client sends
and reads to what the file declares: the path, the method, the query parameters and the
rule for which of them are needed, the state codes, the 200 answer's fields, the 404, the
token URL and the scope.

PyYAML is not a dependency of this project, so the file is read by ``_load_yaml`` below: a
small reader for the block YAML the file uses (mappings, sequences, quoted and plain
scalars, ``|`` / ``|-`` / ``>`` block scalars, ``{}`` and ``[]``). It refuses anything else
rather than guessing, so a construct it does not know fails the test loudly.
"""

from __future__ import annotations

import inspect
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest

from finance import usps
from finance.env_guard import DEFAULT_USPS_API_BASE_URL

SPEC_PATH = Path(__file__).resolve().parents[2] / "docs/integrations/usps/addresses-v3r2_0.yaml"


# --- The reader ---------------------------------------------------------------------------------


class _Line:
    __slots__ = ("indent", "text", "number")

    def __init__(self, indent: int, text: str, number: int) -> None:
        self.indent = indent
        self.text = text
        self.number = number


_BLOCK_SCALAR = re.compile(r"^(?P<head>.*?)(?:^|\s)(?P<style>[|>])(?P<chomp>[-+]?)$")
_KEY = re.compile(r"""^(?P<key>"[^"]*"|'[^']*'|[^\s"'#][^:]*?):(?:\s+(?P<rest>.*))?$""")


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _lines(text: str) -> list[_Line]:
    """Significant lines, with each block scalar folded into one ``<key>: "<json>"`` line."""
    raw = text.splitlines()
    out: list[_Line] = []
    i = 0
    while i < len(raw):
        line = raw[i]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            i += 1
            continue
        if "\t" in line[: len(line) - len(line.lstrip())]:
            raise ValueError(f"line {i + 1}: a tab in the indentation")
        indent = _indent_of(line)
        block = _BLOCK_SCALAR.match(stripped)
        if block and (block["head"].endswith(":") or block["head"] in ("-", "")):
            body: list[str] = []
            j = i + 1
            while j < len(raw) and (not raw[j].strip() or _indent_of(raw[j]) > indent):
                body.append(raw[j])
                j += 1
            margin = min((_indent_of(b) for b in body if b.strip()), default=0)
            joined = "\n".join(b[margin:] for b in body).rstrip("\n")
            if block["style"] == ">":
                joined = re.sub(r"(?<!\n)\n(?!\n)", " ", joined)
            if block["chomp"] != "-":
                joined += "\n"
            out.append(_Line(indent, f"{block['head']} {json.dumps(joined)}".lstrip(), i + 1))
            i = j
            continue
        out.append(_Line(indent, stripped, i + 1))
        i += 1
    return out


def _scalar(text: str, number: int) -> Any:
    if text.startswith('"'):
        if not text.endswith('"') or len(text) < 2:
            raise ValueError(f"line {number}: an unclosed double-quoted scalar")
        return json.loads(text)
    if text.startswith("'"):
        if not text.endswith("'") or len(text) < 2:
            raise ValueError(f"line {number}: an unclosed single-quoted scalar")
        return text[1:-1].replace("''", "'")
    if text == "{}":
        return {}
    if text == "[]":
        return []
    if text[0] in "[{&*!|>%@`":
        raise ValueError(f"line {number}: an unsupported construct {text[:20]!r}")
    if text in ("true", "false"):
        return text == "true"
    if text in ("null", "~"):
        return None
    return text


def _is_item(line: _Line) -> bool:
    return line.text == "-" or line.text.startswith("- ")


def _node(lines: list[_Line], i: int, indent: int) -> tuple[Any, int]:
    if _is_item(lines[i]):
        return _sequence(lines, i, indent)
    return _mapping(lines, i, indent)


def _sequence(lines: list[_Line], i: int, indent: int) -> tuple[list[Any], int]:
    items: list[Any] = []
    while i < len(lines) and lines[i].indent == indent and _is_item(lines[i]):
        line = lines[i]
        content = line.text[1:].lstrip()
        if not content:
            i += 1
            if i >= len(lines) or lines[i].indent <= indent:
                items.append(None)
                continue
            value, i = _node(lines, i, lines[i].indent)
        elif _KEY.match(content):
            # "- key: value": a mapping whose first line sits two columns in.
            lines[i] = _Line(indent + 2, content, line.number)
            value, i = _mapping(lines, i, indent + 2)
        else:
            value, i = _scalar(content, line.number), i + 1
        items.append(value)
    return items, i


def _mapping(lines: list[_Line], i: int, indent: int) -> tuple[dict[str, Any], int]:
    result: dict[str, Any] = {}
    while i < len(lines) and lines[i].indent == indent and not _is_item(lines[i]):
        line = lines[i]
        match = _KEY.match(line.text)
        if match is None:
            raise ValueError(f"line {line.number}: not a key: {line.text[:40]!r}")
        key = match["key"]
        if key[0] in "\"'":
            key = key[1:-1]
        if key in result:
            raise ValueError(f"line {line.number}: the key {key!r} twice")
        rest = match["rest"]
        i += 1
        if rest:
            result[key] = _scalar(rest, line.number)
        elif i < len(lines) and (
            lines[i].indent > indent or (lines[i].indent == indent and _is_item(lines[i]))
        ):
            result[key], i = _node(lines, i, lines[i].indent)
        else:
            result[key] = None
    if i < len(lines) and lines[i].indent > indent:
        raise ValueError(f"line {lines[i].number}: an unexpected indent")
    return result, i


def _load_yaml(text: str) -> Any:
    lines = _lines(text)
    if not lines:
        return None
    value, end = _node(lines, 0, lines[0].indent)
    if end != len(lines):
        raise ValueError(f"line {lines[end].number}: left over")
    return value


# --- The file, resolved -------------------------------------------------------------------------


@pytest.fixture(scope="module")
def spec() -> dict[str, Any]:
    loaded = _load_yaml(SPEC_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _resolve(spec: dict[str, Any], node: Any) -> Any:
    """Follow ``$ref`` (local refs only) until the node is not a reference."""
    while isinstance(node, dict) and "$ref" in node:
        ref = node["$ref"]
        assert ref.startswith("#/"), ref
        target: Any = spec
        for part in ref[2:].split("/"):
            target = target[part]
        node = target
    return node


def _properties(spec: dict[str, Any], schema: Any) -> dict[str, Any]:
    """A schema's properties, ``allOf`` parts merged, each property resolved."""
    schema = _resolve(spec, schema)
    merged: dict[str, Any] = {}
    for part in schema.get("allOf", []):
        merged.update(_properties(spec, part))
    for name, prop in (schema.get("properties") or {}).items():
        merged[name] = _resolve(spec, prop)
    return merged


def _address_operation(spec: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """The server URL and the operation the client calls, found by ``ADDRESS_PATH``."""
    for server in spec["servers"]:
        prefix = urlsplit(server["url"]).path.rstrip("/")
        if usps.ADDRESS_PATH.startswith(prefix + "/"):
            path = usps.ADDRESS_PATH[len(prefix) :]
            if path in spec["paths"]:
                return server["url"], spec["paths"][path]
    raise AssertionError(f"{usps.ADDRESS_PATH} is not a server path plus a path in the file")


def _query_parameters(spec: dict[str, Any], operation: dict[str, Any]) -> dict[str, Any]:
    params = [_resolve(spec, p) for p in operation["get"]["parameters"]]
    return {p["name"]: p for p in params if p["in"] == "query"}


# Every shape of address the client can send: city and ZIP, city only, ZIP only.
SENT = [
    usps._params(usps.Address(street="3120 M St NW", city="Washington", state="DC", zip=zip_))
    for zip_ in ("20007", "")
] + [usps._params(usps.Address(street="3120 M St NW", city="", state="DC", zip="20007-3704"))]


# --- The reader itself --------------------------------------------------------------------------


def test_the_reader_reads_the_constructs_the_file_uses() -> None:
    text = (
        "a:\n"
        "  b: plain text: with a colon\n"
        '  "200":\n'
        "    c: '1''2'\n"
        "  d: |-\n"
        "    one\n"
        "\n"
        "    two\n"
        "  e: {}\n"
        "  f: []\n"
        "  list:\n"
        "    - name: x\n"
        "      required: true\n"
        "    - plain\n"
        "  seq-at-key:\n"
        '  - k: "^\\\\d{5}$"\n'
    )
    assert _load_yaml(text) == {
        "a": {
            "b": "plain text: with a colon",
            "200": {"c": "1'2"},
            "d": "one\n\ntwo",
            "e": {},
            "f": [],
            "list": [{"name": "x", "required": True}, "plain"],
            "seq-at-key": [{"k": "^\\d{5}$"}],
        }
    }


@pytest.mark.parametrize(
    "text",
    [
        "a: [1, 2]\n",
        "a: {b: 1}\n",
        "a: &anchor x\n",
        "a: 1\n  b: 2\n",
        "a: 1\na: 2\n",
        "not a key\n",
    ],
)
def test_the_reader_refuses_what_it_does_not_know(text: str) -> None:
    with pytest.raises(ValueError):
        _load_yaml(text)


def test_the_file_is_the_addresses_api_v3(spec: dict[str, Any]) -> None:
    assert spec["openapi"].startswith("3.0")
    assert spec["info"]["title"] == "Addresses"
    assert set(spec["paths"]) == {"/address", "/city-state", "/zipcode"}


# --- The address call ---------------------------------------------------------------------------


def test_the_address_path_is_the_production_server_plus_the_files_path(
    spec: dict[str, Any],
) -> None:
    server, operation = _address_operation(spec)
    assert server == "https://apis.usps.com/addresses/v3"
    # The client's default base is that server's host; ADDRESS_PATH carries the rest.
    assert DEFAULT_USPS_API_BASE_URL + usps.ADDRESS_PATH == server + "/address"
    # The client sends a GET, and the file's operation is a GET.
    assert set(operation) == {"get"}
    assert '"GET", base + ADDRESS_PATH' in inspect.getsource(usps.check)


@pytest.mark.parametrize("params", SENT, ids=["city-and-zip", "city", "zip"])
def test_every_parameter_the_client_sends_is_the_operations(
    spec: dict[str, Any], params: dict[str, str] | None
) -> None:
    assert params is not None
    _, operation = _address_operation(spec)
    declared = _query_parameters(spec, operation)
    assert set(params) <= set(declared), set(params) - set(declared)
    # Every required parameter is sent.
    assert {name for name, p in declared.items() if p.get("required")} <= set(params)
    # The file's "a city and a state, or a ZIP Code" rule is met.
    alternatives = [set(alt["required"]) for alt in operation["get"]["x-anyOf-required"]]
    assert any(alt <= set(params) for alt in alternatives), (alternatives, set(params))
    # Each value fits its parameter's pattern and length.
    for name, value in params.items():
        schema = declared[name]["schema"]
        if "pattern" in schema:
            assert re.search(schema["pattern"], value), (name, schema["pattern"])
        if "maxLength" in schema:
            assert len(value) <= int(schema["maxLength"]), name


def test_the_client_sends_exactly_the_four_parameters_d14_names() -> None:
    assert set().union(*(p or {} for p in SENT)) == {"streetAddress", "city", "state", "ZIPCode"}


def test_the_clients_state_codes_are_the_files(spec: dict[str, Any]) -> None:
    _, operation = _address_operation(spec)
    parameter = _query_parameters(spec, operation)["state"]["schema"]["pattern"]
    schema = spec["components"]["schemas"]["State"]["pattern"]
    for pattern in (parameter, schema):
        codes = re.fullmatch(r"\^\(\?:(.*)\)\$", pattern)
        assert codes is not None, pattern
        assert set(codes[1].split("|")) == usps.USPS_STATES


def _client_reads(name: str) -> set[str]:
    """The keys ``_checked`` reads with ``<name>.get("...")``."""
    return set(re.findall(rf'{name}\.get\("(\w+)"\)', inspect.getsource(usps._checked)))


def test_the_answer_fields_the_client_reads_are_in_the_200_schema(spec: dict[str, Any]) -> None:
    _, operation = _address_operation(spec)
    ok = operation["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    top = _properties(spec, ok)
    assert {"address", "additionalInfo"} <= set(top)
    address_fields = _client_reads("found")
    assert address_fields == {
        "streetAddress",
        "secondaryAddress",
        "city",
        "state",
        "ZIPCode",
        "ZIPPlus4",
    }
    assert address_fields <= set(_properties(spec, top["address"]))
    info_fields = _client_reads("info")
    assert info_fields == {"DPVConfirmation"}
    info = _properties(spec, top["additionalInfo"])
    assert info_fields <= set(info)
    # "Y" is the value the client takes for confirmed.
    assert "* `Y` - Address was DPV confirmed" in info["DPVConfirmation"]["description"]


def test_the_answers_zip_fields_match_the_clients_checks(spec: dict[str, Any]) -> None:
    _, operation = _address_operation(spec)
    ok = operation["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    address = _properties(spec, _properties(spec, ok)["address"])
    assert address["ZIPCode"]["pattern"] == "\\d{5}" and usps._ZIP5.pattern == "[0-9]{5}"
    assert address["ZIPPlus4"]["pattern"] == "\\d{4}" and usps._ZIP4.pattern == "[0-9]{4}"
    # ZIPPlus4 may be null; the client reads a null as no ZIP+4.
    assert address["ZIPPlus4"]["nullable"] is True
    assert usps._text(None) == ""


def test_a_404_answers_address_not_found_and_a_401_is_declared(spec: dict[str, Any]) -> None:
    _, operation = _address_operation(spec)
    responses = operation["get"]["responses"]
    examples = responses["404"]["content"]["application/json"]["examples"]
    assert examples["Address Not Found"] == {"$ref": "#/components/examples/Address-Not-Found"}
    assert "401" in responses


# --- The token ----------------------------------------------------------------------------------


def test_the_token_url_and_scope_are_the_files(spec: dict[str, Any]) -> None:
    flows = spec["components"]["securitySchemes"]["OAuth"]["flows"]
    assert spec["components"]["securitySchemes"]["OAuth"]["type"] == "oauth2"
    # The file's tokenUrl is relative and starts with "/": it resolves against the server's
    # host, https://apis.usps.com/oauth2/v3/token, the URL the client posts to.
    assert flows["clientCredentials"]["tokenUrl"] == usps.TOKEN_PATH == "/oauth2/v3/token"
    assert "addresses" in flows["clientCredentials"]["scopes"]
    server, operation = _address_operation(spec)
    parts = urlsplit(server)
    origin = f"{parts.scheme}://{parts.netloc}"
    assert origin + flows["clientCredentials"]["tokenUrl"] == (
        DEFAULT_USPS_API_BASE_URL + usps.TOKEN_PATH
    )
    assert operation["get"]["security"] == [{"OAuth": ["addresses"]}]
    assert '"client_credentials"' in inspect.getsource(usps._token)

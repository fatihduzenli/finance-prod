"""The one-line help rule (docs/qa/PLAN-012e2-fewer-actions.md D10, test plan §6 C).

Every template under ``ledger/templates/ledger`` and ``finance/templates`` is read as HTML.
A *block* is a ``<section>``, a ``<fieldset>``, an element whose class holds ``card``, or the
content between two ``<h2>`` inside the same one of those (or the page). In a block, the
explanatory text is a ``<p>`` whose class holds ``muted`` or ``help``, or any element whose
class holds ``help``; an empty state (``.empty``), an error list (``.errorlist``) and a form
control's help text (inside a ``.field``) are not explanatory text, and neither is an element
with no word of its own outside its links and its ``{{ values }}`` (a breadcrumb of links, a
status message the view words) or a line of facts joined by "·" (the store context line).
A block holds at most one, and no sentence of one is longer than :data:`SENTENCE_LIMIT`
characters.

Template branches are alternatives: the paragraphs of ``{% if %}`` / ``{% elif %}`` /
``{% else %}`` (and of a ``{% for %}`` and its ``{% empty %}``) are counted per branch, and a
block holds the most any one branch gives it. ``{% comment %}`` blocks are not read. In the
text, ``{{ value }}`` counts as one character and a ``{% tag %}`` as none; the branches of a
``{% if %}`` inside one element are measured one at a time.

What breaks the rule moves to the matching help page (``finance/templates/finance/help/``)
or is shortened to one line.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
ROOTS = (BACKEND / "ledger" / "templates" / "ledger", BACKEND / "finance" / "templates")
#: The longest sentence a line of help may hold.
SENTENCE_LIMIT = 160
VOID = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "wbr"}
)
_COMMENT = re.compile(r"{%\s*comment\s*%}.*?{%\s*endcomment\s*%}|{#.*?#}", re.S)
_TAG = re.compile(r"{%\s*(\w+)[^%]*%}")
_VARIABLE = re.compile(r"{{.*?}}", re.S)
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
_ALTERNATIVE = re.compile(r"{%\s*(?:elif|else|empty)\b[^%]*%}")
_WORD = re.compile(r"[A-Za-z]")
#: The separator of a line of facts ("Store 0202 · Lease from 2026-01-01 · Operator …").
FACTS_SEPARATOR = "\u00b7"
BRANCH_OPEN = frozenset({"if", "for"})
BRANCH_NEXT = frozenset({"elif", "else", "empty"})
BRANCH_CLOSE = frozenset({"endif", "endfor"})


@dataclass(frozen=True)
class Finding:
    template: str
    line: int
    text: str
    reason: str

    def __str__(self) -> str:
        return f"{self.template}:{self.line}: {self.reason}: {self.text!r}"


@dataclass
class _Element:
    tag: str
    classes: frozenset[str]
    block: int | None
    explanatory: bool = False
    text: list[str] = field(default_factory=list)
    #: The text outside the element's links.
    own: list[str] = field(default_factory=list)
    line: int = 0
    segment: tuple[int, int] = (0, 0)


@dataclass
class _Branch:
    """An ``{% if %}`` or ``{% for %}``: the counts when it opened, and the most each block
    got from one of its branches so far."""

    start: dict[tuple[int, int], int]
    best: dict[tuple[int, int], int] = field(default_factory=dict)


def _classes(attrs: list[tuple[str, str | None]]) -> frozenset[str]:
    value = next((v or "" for k, v in attrs if k == "class"), "")
    return frozenset(_TAG.sub(" ", value).split())


def _visible(text: str) -> str:
    return " ".join(_TAG.sub("", _VARIABLE.sub("X", text)).split())


class _Scanner(HTMLParser):
    def __init__(self, template: str) -> None:
        super().__init__(convert_charrefs=True)
        self.template = template
        self.stack: list[_Element] = []
        #: Block container ids (0 is the page) and the h2 count inside each.
        self.next_block = 1
        self.h2: dict[int, int] = {0: 0}
        self.counts: dict[tuple[int, int], int] = {}
        self.branches: list[_Branch] = []
        #: Each line of help, with how many its block held up to it on its branch.
        self.paragraphs: list[tuple[int, _Element]] = []
        self.findings: list[Finding] = []

    # --- where an element is ------------------------------------------------------------

    def _container(self) -> int:
        for element in reversed(self.stack):
            if element.block is not None:
                return element.block
        return 0

    def _segment(self) -> tuple[int, int]:
        container = self._container()
        return (container, self.h2[container])

    def _inside_field(self) -> bool:
        return any("field" in element.classes for element in self.stack)

    def _in_explanatory(self) -> _Element | None:
        return next((element for element in self.stack if element.explanatory), None)

    # --- parsing ------------------------------------------------------------------------

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        classes = _classes(attrs)
        if tag == "h2":
            container = self._container()
            self.h2[container] += 1
        if tag in VOID:
            return
        block = None
        if tag in ("section", "fieldset") or "card" in classes:
            block = self.next_block
            self.next_block += 1
            self.h2[block] = 0
        element = _Element(tag, classes, block, line=self.getpos()[0])
        element.explanatory = (
            (tag == "p" and bool(classes & {"muted", "help"}) or "help" in classes)
            and not classes & {"empty", "errorlist"}
            and not self._inside_field()
            and self._in_explanatory() is None
        )
        element.segment = self._segment()
        self.stack.append(element)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "h2":
            self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag in VOID:
            return
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index].tag == tag:
                for element in self.stack[index:]:
                    self._closed(element)
                del self.stack[index:]
                return

    def _closed(self, element: _Element) -> None:
        own = _TAG.sub("", _VARIABLE.sub("", "".join(element.own)))
        if element.explanatory and _WORD.search(own) and FACTS_SEPARATOR not in own:
            self.counts[element.segment] = self.counts.get(element.segment, 0) + 1
            self.paragraphs.append((self.counts[element.segment], element))

    def handle_data(self, data: str) -> None:
        owner = self._in_explanatory()
        if owner is not None:
            owner.text.append(data)
            if not any(element.tag == "a" for element in self.stack):
                owner.own.append(data)
        for match in _TAG.finditer(data):
            self._template_tag(match.group(1))

    # --- template branches --------------------------------------------------------------

    def _template_tag(self, name: str) -> None:
        if name in BRANCH_OPEN:
            self.branches.append(_Branch(start=dict(self.counts)))
        elif name in BRANCH_NEXT and self.branches:
            self._close_branch(self.branches[-1])
            self.counts = dict(self.branches[-1].start)
        elif name in BRANCH_CLOSE and self.branches:
            branch = self.branches.pop()
            self._close_branch(branch)
            merged = dict(branch.start)
            for segment, count in branch.best.items():
                merged[segment] = max(merged.get(segment, 0), count)
            self.counts = merged

    def _close_branch(self, branch: _Branch) -> None:
        for segment, count in self.counts.items():
            branch.best[segment] = max(branch.best.get(segment, 0), count)

    # --- the findings -------------------------------------------------------------------

    def finish(self) -> list[Finding]:
        for ordinal, element in self.paragraphs:
            raw = "".join(element.text)
            text = _visible(raw)
            if ordinal > 1:
                self.findings.append(
                    Finding(self.template, element.line, text, "a second line of help")
                )
            sentences = [
                sentence
                for branch in _ALTERNATIVE.split(raw)
                for sentence in _SENTENCE_END.split(_visible(branch))
            ]
            for sentence in sentences:
                if len(sentence) > SENTENCE_LIMIT:
                    self.findings.append(
                        Finding(
                            self.template,
                            element.line,
                            sentence,
                            f"a sentence of {len(sentence)} characters",
                        )
                    )
        return self.findings


def scan(source: str, template: str = "<template>") -> list[Finding]:
    """The findings of one template's source."""
    scanner = _Scanner(template)
    scanner.feed(_COMMENT.sub("", source))
    scanner.close()
    return scanner.finish()


def templates() -> list[Path]:
    return sorted(path for root in ROOTS for path in root.rglob("*.html"))


# --- the rule over every template ------------------------------------------------------------


def test_every_template_keeps_one_short_line_of_help_per_block() -> None:
    findings = [
        finding
        for path in templates()
        for finding in scan(path.read_text(encoding="utf-8"), str(path.relative_to(BACKEND)))
    ]
    assert findings == [], "\n" + "\n".join(str(finding) for finding in findings)


def test_the_walk_reads_both_template_roots() -> None:
    found = {path.relative_to(BACKEND).parts[0] for path in templates()}
    assert found == {"ledger", "finance"}
    assert len(templates()) > 100


# --- the scanner itself ----------------------------------------------------------------------


def test_a_second_line_in_a_section_is_found() -> None:
    source = '<section><p class="muted">One.</p><p class="help">Two.</p></section>'
    [finding] = scan(source)
    assert finding.reason == "a second line of help" and finding.text == "Two."


def test_a_second_line_between_two_h2_is_found_and_a_new_h2_starts_a_new_block() -> None:
    source = (
        '<h2>A</h2><p class="muted">One.</p><p class="muted">Two.</p>'
        '<h2>B</h2><p class="muted">Three.</p>'
    )
    assert [finding.text for finding in scan(source)] == ["Two."]


def test_a_card_and_a_fieldset_are_blocks_of_their_own() -> None:
    source = (
        '<h2>A</h2><p class="muted">One.</p>'
        '<div class="card"><p class="muted">Two.</p></div>'
        '<fieldset><p class="muted">Three.</p></fieldset>'
    )
    assert scan(source) == []


def test_any_element_with_the_help_class_counts_but_a_muted_span_does_not() -> None:
    assert len(scan('<section><span class="help">a</span><div class="help">b</div></section>'))
    assert scan('<section><p class="muted">a</p><span class="muted">b</span></section>') == []


def test_an_empty_state_an_error_list_and_a_field_help_text_are_not_counted() -> None:
    source = (
        '<section><p class="muted">One.</p><p class="muted empty">Nothing yet.</p>'
        '<ul class="errorlist help"><li>x</li></ul>'
        '<div class="field"><label>A</label><span class="help muted">Help.</span></div>'
        "</section>"
    )
    assert scan(source) == []


def test_branches_are_alternatives_but_two_lines_in_one_branch_are_not() -> None:
    alternatives = (
        '<section>{% if a %}<p class="muted">One.</p>{% elif b %}<p class="muted">Two.</p>'
        '{% else %}<p class="muted">Three.</p>{% endif %}</section>'
    )
    assert scan(alternatives) == []
    both = (
        '<section>{% if a %}<p class="muted">One.</p><p class="muted">Two.</p>{% endif %}</section>'
    )
    assert len(scan(both)) == 1
    after = (
        '<section>{% if a %}<p class="muted">One.</p>{% endif %}<p class="muted">Two.</p></section>'
    )
    assert len(scan(after)) == 1


def test_a_long_sentence_is_found_and_a_variable_counts_as_one_character() -> None:
    long = "word " * 40
    [finding] = scan(f'<p class="muted">Short. {long.strip()}.</p>')
    assert finding.reason.startswith("a sentence of ")
    short = "{{ a_very_long_variable_name_that_is_not_shown|default:'something' }} " * 12
    assert scan(f'<p class="muted">{short}</p>') == []


def test_a_breadcrumb_of_links_and_a_message_of_the_view_are_not_counted() -> None:
    source = (
        '<section><p class="muted"><a href="/a">Lease</a> · <a href="/b">Daily</a></p>'
        '<p class="muted">{{ retry_wait_message }}</p><p class="muted">One.</p></section>'
    )
    assert scan(source) == []
    assert len(
        scan('<section><p class="muted">One.</p><p class="muted">Two <a>x</a></p></section>')
    )


def test_a_line_of_facts_is_not_counted() -> None:
    source = (
        '<section><p class="muted">Store {{ s }} · Lease from {{ d }}</p>'
        '<p class="muted">One.</p></section>'
    )
    assert scan(source) == []


def test_a_comment_is_not_read() -> None:
    source = (
        '{% comment %}<section><p class="muted">a</p><p class="muted">b</p></section>'
        "{% endcomment %}"
    )
    assert scan(source) == []


def test_a_deliberate_second_paragraph_in_a_copy_of_the_store_page_fails() -> None:
    # PLAN-012f D8: the store cost block is the STORE COST side card (stores/_header.html).
    page = (BACKEND / "ledger/templates/ledger/stores/_header.html").read_text(encoding="utf-8")
    assert scan(page) == []
    heading = '<h2 class="section-label" id="store-cost-heading">Store cost</h2>'
    mutated = page.replace(heading, heading + '<p class="muted">A second line of help.</p>')
    assert mutated != page
    assert any(finding.reason == "a second line of help" for finding in scan(mutated))


@pytest.mark.parametrize("path", templates(), ids=lambda path: path.name)
def test_every_template_parses(path: Path) -> None:
    scanner = _Scanner(path.name)
    scanner.feed(_COMMENT.sub("", path.read_text(encoding="utf-8")))
    scanner.close()

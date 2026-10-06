"""Adversarial cases for PLAN-011 (dashboard, grid, arrears, reports, exports, PWA), by the
QA owner (plan §6). Opus's files prove the arithmetic and the markup; these cases execute the
service worker against fake requests, cross-check totals with an independent generator, and
probe the exports and the remedy action from the outside."""

from __future__ import annotations

import csv
import io
import json
import random
import shutil
import subprocess
import uuid
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from django.test import Client
from django.utils import timezone

from accounts.roles import Role
from ledger.models import (
    CollectionStatus,
    Lease,
    Payment,
    PaymentChannel,
    PaymentKind,
    PaymentMethod,
    PaymentStatus,
    RentCharge,
    RentChargeStatus,
    Store,
    StoreStatus,
)
from ledger.services import receivables
from tests.test_lease_services import make_lease, make_operator
from tests.test_operator_views import client_for
from tests.test_pwa import worker as worker_source

pytestmark = pytest.mark.django_db

D = Decimal
NODE = shutil.which("node")

# --- the service worker, executed ----------------------------------------------------------------

HARNESS = r"""
const ORIGIN = "https://app.example.test";
const listeners = {};
const log = { opened: [], addAll: [], put: [], fetched: [], responded: [], deleted: [] };
globalThis.self = {
  addEventListener: (type, fn) => { listeners[type] = fn; },
  location: { origin: ORIGIN },
  skipWaiting: async () => {},
  clients: { claim: async () => {} },
};
const cache = {
  addAll: async (reqs) => { log.addAll.push(...reqs.map((r) => r.url)); },
  match: async () => undefined,
  put: async (req) => { log.put.push(req.url); },
};
globalThis.caches = {
  open: async (name) => { log.opened.push(name); return cache; },
  keys: async () => ["finance-static-old", "something-else"],
  delete: async (name) => { log.deleted.push(name); return true; },
};
globalThis.fetch = async (req) => {
  log.fetched.push(req.url);
  return {
    ok: true, status: 200, type: "basic",
    headers: { get: (k) => (k.toLowerCase() === "cache-control" ? "private, no-store" : null) },
    clone() { return this; },
  };
};
globalThis.Request = class {
  constructor(url, init) { this.url = url; Object.assign(this, init || {}); }
};
__WORKER__
async function dispatch(request) {
  let promise = null;
  listeners.fetch({ request, respondWith: (p) => { promise = p; } });
  if (promise) { await promise; log.responded.push(request.url); }
}
(async () => {
  await new Promise((r) => listeners.install({ waitUntil: (p) => p.then(r) }));
  await new Promise((r) => listeners.activate({ waitUntil: (p) => p.then(r) }));
  const requests = JSON.parse(process.argv[2]);
  for (const r of requests) { await dispatch(r); }
  console.log(JSON.stringify(log));
})();
"""


@pytest.mark.skipif(NODE is None, reason="node is not installed here")
def test_the_worker_never_answers_or_stores_anything_dynamic(tmp_path: Path) -> None:
    source = worker_source()
    origin = "https://app.example.test"
    precached = json.loads(source.split("const PRECACHE = ", 1)[1].split(";", 1)[0])
    static_path = precached[0] if precached else "/static/finance/app.css"
    requests = [
        {"method": "GET", "url": f"{origin}/", "mode": "navigate"},
        {"method": "GET", "url": f"{origin}/grid/", "mode": "navigate"},
        {"method": "GET", "url": f"{origin}/grid/?from=2026-01&to=2026-12", "mode": "cors"},
        {"method": "GET", "url": f"{origin}/reports/aging/export.csv", "mode": "cors"},
        {"method": "GET", "url": f"{origin}/healthz", "mode": "cors"},
        {"method": "GET", "url": f"{origin}/documents/{uuid.uuid4()}/download/", "mode": "cors"},
        {"method": "GET", "url": f"{origin}/payments/", "mode": "no-cors"},
        {"method": "POST", "url": f"{origin}{static_path}", "mode": "cors"},
        {"method": "GET", "url": f"{origin}{static_path}?v=1", "mode": "cors"},
        {"method": "GET", "url": f"https://evil.example.test{static_path}", "mode": "cors"},
        {"method": "GET", "url": f"{origin}{static_path}", "mode": "cors"},
    ]
    script = tmp_path / "sw_harness.js"
    script.write_text(HARNESS.replace("__WORKER__", source))
    run = subprocess.run(
        [NODE or "node", str(script), json.dumps(requests)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert run.returncode == 0, run.stderr
    log = json.loads(run.stdout.strip().splitlines()[-1])
    # Only the allow-listed static GET on our origin is answered by the worker.
    expected = [f"{origin}{static_path}"] if precached else []
    assert log["responded"] == expected
    assert log["fetched"] == expected
    # Its response was marked private/no-store, so even the static branch stored nothing.
    assert log["put"] == []
    # Install precaches exactly the allow-list; activate deletes every other cache.
    assert log["addAll"] == [f"{origin}{p}" if p.startswith("/") else p for p in precached] or (
        log["addAll"] == precached
    )
    assert set(log["deleted"]) == {"finance-static-old", "something-else"}


def test_the_precache_list_names_only_static_files_and_no_page() -> None:
    source = worker_source()
    precached = json.loads(source.split("const PRECACHE = ", 1)[1].split(";", 1)[0])
    for path in precached:
        assert path.startswith("/static/"), path
        assert not path.endswith((".html", ".csv", ".json")) or path.endswith(".webmanifest")
    for forbidden in ("/grid", "/reports", "/healthz", "/documents", "/payments", "/audit"):
        assert not any(p.startswith(forbidden) for p in precached)


# --- aging equals outstanding, with an independent generator ------------------------------------


def _random_world(seed: int) -> date:
    rng = random.Random(seed)  # noqa: S311 - test data, not security
    today = date(2026, 10, 6)
    for n in range(rng.randint(1, 6)):
        operator = make_operator(email=f"adv11-{seed}-{n}@example.com", customer=None)
        lease = make_lease(
            D(str(rng.randint(500, 9000))) + D("0.50"),
            operator=operator,
            started_on=date(2025, 1, 1),
        )
        for months_ago in range(rng.randint(1, 9)):
            due = (today.replace(day=1) - timedelta(days=30 * months_ago)).replace(
                day=rng.choice([1, 15, 28])
            )
            amount = D(str(rng.randint(100, 5000))) + D(f"0.{rng.randint(0, 99):02d}")
            status = rng.choice(
                [
                    RentChargeStatus.DUE,
                    RentChargeStatus.UNPAID,
                    RentChargeStatus.PAID,
                    RentChargeStatus.PARTIAL,
                    RentChargeStatus.PENDING,
                    RentChargeStatus.WAIVED,
                ]
            )
            paid = {
                RentChargeStatus.PAID: amount,
                RentChargeStatus.PARTIAL: (amount / 3).quantize(D("0.01")),
            }.get(status, D("0.00"))
            RentCharge.objects.create(
                lease=lease,
                period_month=due.replace(day=1),
                seq=rng.randint(1, 2) if due.day != 1 else 1,
                due_on=due,
                amount_due=amount,
                amount_paid=paid,
                status=status,
            )
    return today


@pytest.mark.parametrize("seed", range(12))
def test_aging_total_equals_outstanding_total_under_random_data(seed: int) -> None:
    today = _random_world(seed)
    balances = receivables.open_balances(today)
    outstanding = sum((row.balance for row in balances), D("0.00"))
    aging = receivables.aging(today)
    grand = getattr(aging, "grand_total", None)
    if grand is None:  # the structure is Opus's; find the total however it is named
        grand = sum((getattr(row, "total", D(0)) for row in getattr(aging, "rows", [])), D(0))
    assert grand == outstanding
    assert all(row.balance > 0 for row in balances)
    assert not any(
        row.kind == "rent" and RentCharge.objects.get(pk=row.charge_id).status == "waived"
        for row in balances
        if hasattr(row, "charge_id")
    )


# --- exports and pages from the outside ---------------------------------------------------------


def _store_with_hostile_names() -> Store:
    operator = make_operator(email="hostile-grid@example.com", customer=None)
    operator.display_name = '=HYPERLINK("https://evil.example.test")'
    operator.save(update_fields=["display_name"])
    store = Store.objects.create(
        external_label="+1 Store", store_number=4242, state="@TX", status=StoreStatus.ACTIVE
    )
    Lease.objects.create(
        store=store,
        operator=operator,
        debit_day=1,
        payment_channel=PaymentChannel.WIRE,
        collection_status=CollectionStatus.ACTIVE,
        started_on=date(2026, 1, 1),
    )
    return store


def test_every_new_csv_is_formula_safe_whatever_the_names() -> None:
    _store_with_hostile_names()
    viewer, _ = client_for(Role.VIEWER)
    for path in (
        "/grid/export.csv?from=2026-01&to=2026-03",
        "/arrears/export.csv",
        "/reports/aging/export.csv",
        "/stores/export.csv",
        "/operators/export.csv",
    ):
        response = viewer.get(path)
        assert response.status_code == 200, path
        body = b"".join(response.streaming_content).decode()  # type: ignore[attr-defined]
        for row in csv.reader(io.StringIO(body)):
            for cell in row:
                assert not cell.startswith(("=", "+", "-", "@")), (path, cell)
        assert "private" in response["Cache-Control"] and "no-store" in response["Cache-Control"]


def test_the_remedy_action_is_closed_to_everyone_but_an_admin() -> None:
    for role in (Role.VIEWER, Role.BILLING, None):
        client, _ = client_for(role)
        response = client.post(f"/one-off-charges/{uuid.uuid4()}/delete-invoice/", {})
        assert response.status_code in (403, 404), role
    anonymous = Client()
    assert anonymous.post(f"/one-off-charges/{uuid.uuid4()}/delete-invoice/", {}).status_code in (
        302,
        401,
        403,
    )


@pytest.mark.parametrize(
    "params",
    [
        {"from": "0001-01", "to": "9999-12"},
        {"from": "2026-13", "to": "2026-00"},
        {"operator": "' OR 1=1 --", "state": "<script>", "channel": "../", "status": "x" * 5000},
        {"from": "2026-01", "to": "2030-12"},
    ],
)
def test_hostile_grid_and_arrears_parameters_answer_200(params: dict[str, str]) -> None:
    viewer, _ = client_for(Role.VIEWER)
    for path in ("/grid/", "/arrears/", "/reports/aging/", "/"):
        response = viewer.get(path, params)
        assert response.status_code == 200, (path, params)
        assert b"Traceback" not in response.content


def test_a_statement_shows_only_its_operators_money() -> None:
    first = make_operator(email="first-stmt@example.com", customer=None)
    second = make_operator(email="second-stmt@example.com", customer=None)
    lease_a = make_lease(D("1000.00"), operator=first, started_on=date(2026, 1, 1))
    lease_b = make_lease(D("2000.00"), operator=second, started_on=date(2026, 1, 1))
    for lease, amount in ((lease_a, D("1111.11")), (lease_b, D("2222.22"))):
        charge = RentCharge.objects.create(
            lease=lease,
            period_month=date(2026, 9, 1),
            seq=1,
            due_on=date(2026, 9, 1),
            amount_due=amount,
            status=RentChargeStatus.UNPAID,
        )
        Payment.objects.create(
            rent_charge=charge,
            kind=PaymentKind.RENT,
            method=PaymentMethod.WIRE,
            amount=D("1.00"),
            status=PaymentStatus.SETTLED,
            settled_on=timezone.localdate(),
            note=f"note {amount}",
        )
    viewer, _ = client_for(Role.VIEWER)
    page = viewer.get(f"/operators/{first.pk}/statement/").content.decode()
    assert "1,111.11" in page or "1111.11" in page
    assert "2,222.22" not in page and "2222.22" not in page
    assert "note 2222.22" not in page

"""The dispute list and its evidence (docs/qa/PLAN-012-contact-log-invoice-builder-disputes.md
D8, D9; §6 items of Batch C; the adversarial cases are the QA owner's file).

The provider calls through the real transport (``httpx.MockTransport``): the list query
and the multipart upload. The service against a :class:`~payments.fake.FakeProvider`
through the job queue: the sync (pages, upsert, payment link, verbatim statuses, the
provider down), Refresh, the daily tick and the dispute webhooks; evidence through the
10c documents path (an in-memory storage) and the worker's upload; the page, its CSV and
its role gates. Nothing reaches a network.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from email.parser import BytesParser
from email.policy import HTTP
from io import StringIO
from typing import Any

import httpx
import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import override_settings

import documents_storage
from accounts.roles import Role
from documents_storage.memory import MemoryDocumentStorage
from ledger.models import (
    AuditLog,
    Dispute,
    DisputeEvidence,
    DisputeEvidenceStatus,
    Document,
    DocumentKind,
    Operator,
    Payment,
    RentChargeStatus,
)
from ledger.services import disputes
from ledger.wording import provider_name
from payments.errors import ProviderRejected, ProviderUnavailable, ValidationFailed
from payments.fake import RESPOND_BY_PASSED, FakeProvider
from payments.models import ExternalRef, Job, JobKind, JobStatus, SyncState
from payments.provider import (
    EVIDENCE_MAX_BYTES,
    DateRange,
    DisputeQuery,
    DisputeRecord,
    mask_identifier,
)
from payments.stax.adapter import StaxProvider
from tests.document_files import JPEG, PNG, make_docx, make_pdf, paragraph, part
from tests.job_helpers import run_queued_jobs
from tests.stax_helpers import always, make_rig, sequence
from tests.test_operator_views import client_for
from tests.test_webhook_handlers import (
    SETTLED_AT,
    TXN,
    World,
    deliver,
    make_world,
    provider_txn,
)

pytestmark = pytest.mark.django_db

D = Decimal
NOW = datetime(2026, 10, 6, 16, 0, tzinfo=UTC)  # 12:00 in New York
TODAY = NOW.date()
ROUTING = "123456780"
ACCOUNT = "987654321012"
TIFF = b"II*\x00" + b"\x00" * 64
#: ``TXN`` as the page shows it (eight characters: fully masked).
MASKED = mask_identifier(TXN)
PAGE = "/disputes/"
DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


@pytest.fixture(autouse=True)
def fixed_now(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("django.utils.timezone.now", lambda: NOW)


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeProvider:
    provider = FakeProvider()
    monkeypatch.setattr("payments.get_provider", lambda: provider)
    return provider


@pytest.fixture(autouse=True)
def storage(monkeypatch: pytest.MonkeyPatch) -> MemoryDocumentStorage:
    memory = MemoryDocumentStorage()
    monkeypatch.setattr(documents_storage, "get_storage", lambda: memory)
    return memory


@pytest.fixture(autouse=True)
def worker() -> Iterator[None]:
    with override_settings(STAX_CALLER="worker"):
        yield


# --- Builders ----------------------------------------------------------------------------


def a_dispute(
    fake: FakeProvider,
    external_id: str = "dsp-1",
    *,
    respond_by: datetime | None = NOW + timedelta(days=10),
    status: str = "OPEN",
    transaction: str = TXN,
    **fields: Any,
) -> DisputeRecord:
    values: dict[str, Any] = {
        "reason": "FRAUD",
        "amount": D("1500.00"),
        "created_at": NOW - timedelta(days=2),
        "updated_at": NOW - timedelta(days=1),
        "raw_summary": {"status": status, "reason": "FRAUD", "amount": "1500.00"},
    }
    return fake.add_dispute(
        DisputeRecord(
            external_id=external_id,
            raw_status=status,
            fetched_at=NOW,
            transaction_external_id=transaction,
            respond_by=respond_by,
            **{**values, **fields},
        )
    )


def a_paid_world(fake: FakeProvider) -> World:
    """A rent charge paid by the settled transaction ``TXN``."""
    world = make_world(fake)
    provider_txn(fake, settled_at=SETTLED_AT)
    assert deliver("update_transaction_settled", id=TXN) == "processed"
    return world


def synced(fake: FakeProvider) -> disputes.SyncResult:
    result = disputes.sync()
    assert result.ok, result.message
    return result


def the_dispute(external_id: str = "dsp-1") -> Dispute:
    ref = ExternalRef.objects.get(external_kind="dispute", external_id=external_id)
    return Dispute.objects.get(pk=ref.entity_id)


def admin() -> Any:
    return client_for(Role.ADMIN)[1]


def a_file(name: str, data: bytes) -> SimpleUploadedFile:
    return SimpleUploadedFile(name, data, content_type="application/octet-stream")


def png_of(size: int) -> bytes:
    return PNG + b"\x00" * (size - len(PNG))


def add(
    dispute: Dispute, name: str, data: bytes, *, acknowledged: bool = True, actor: Any = None
) -> disputes.Outcome:
    return disputes.add_evidence(
        dispute,
        a_file(name, data),
        "Signed lease page",
        actor or admin(),
        acknowledged=acknowledged,
    )


def ready(fake: FakeProvider, **fields: Any) -> Dispute:
    """A synced dispute linked to our paid payment."""
    a_paid_world(fake)
    a_dispute(fake, **fields)
    synced(fake)
    return the_dispute()


# --- The provider interface through the real transport -----------------------------------


def test_list_disputes_sends_the_verified_query_and_keeps_statuses_verbatim() -> None:
    body = {
        "current_page": 1,
        "data": [
            {
                "id": "dsp-9",
                "transaction_id": "txn-9",
                "merchant_id": "merchant-000011112222",
                "reason": "QUALITY",
                "amount": 1250.5,
                "status": "PREARBITRATION",
                "respond_by": "2026-10-20 23:59:59",
                "created_at": "2026-10-01 10:00:00",
                "updated_at": "2026-10-02 11:00:00",
            }
        ],
    }
    rig = make_rig(always(200, body))
    query = DisputeQuery(DateRange(TODAY - timedelta(days=90), TODAY), page=2)
    [record] = StaxProvider(rig.transport).list_disputes(query)
    [request] = rig.recorder.requests
    assert (request.method, request.url.path) == ("GET", "/underwriting/disputes")
    assert dict(request.url.params) == {
        "startDate": "2026-07-08",
        "endDate": "2026-10-06",
        "page": "2",
        "sort": "respond_by",
    }
    assert (record.external_id, record.transaction_external_id) == ("dsp-9", "txn-9")
    assert (record.raw_status, record.reason, record.amount) == (
        "PREARBITRATION",
        "QUALITY",
        D("1250.5"),
    )
    assert record.respond_by == datetime(2026, 10, 20, 23, 59, 59, tzinfo=UTC)
    assert "merchant_id" not in record.raw_summary and "id" not in record.raw_summary
    assert "transaction_id" not in record.raw_summary


def test_list_disputes_sends_a_status_filter_and_refuses_a_bad_one() -> None:
    rig = make_rig(always(200, {"data": []}))
    provider = StaxProvider(rig.transport)
    provider.list_disputes(DisputeQuery(DateRange(TODAY - timedelta(days=1), TODAY), "OPEN"))
    assert rig.recorder.requests[0].url.params["status"] == "OPEN"
    with pytest.raises(ValidationFailed):
        provider.list_disputes(DisputeQuery(DateRange(TODAY - timedelta(days=1), TODAY), "o&x"))
    assert len(rig.recorder.requests) == 1


def _parts(request: httpx.Request) -> dict[str, tuple[str, bytes, str | None]]:
    """name -> (filename or "", bytes, content type) of a multipart body."""
    content_type = request.headers["content-type"]
    message = BytesParser(policy=HTTP).parsebytes(
        b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + request.content
    )
    found = {}
    for item in message.iter_parts():
        name = item.get_param("name", header="content-disposition")
        payload = item.get_payload(decode=True)
        assert isinstance(name, str) and isinstance(payload, bytes)
        filename = item.get_filename() or ""
        part_type = item.get_content_type() if item.get("content-type") else None
        found[name] = (filename, payload, part_type)
    return found


def test_upload_sends_one_multipart_request_with_the_three_fields() -> None:
    data = b"%PDF-1.4\r\n--not-a-boundary\r\n" + bytes(range(256))
    rig = make_rig(always(200, {"id": "file-77", "status": "UPLOADED"}))
    receipt = StaxProvider(rig.transport).upload_dispute_evidence(
        "dsp-1", "Signed lease page 3", data, "application/pdf"
    )
    [request] = rig.recorder.requests
    assert (request.method, request.url.path) == ("POST", "/file/dispute")
    assert request.headers["content-type"].startswith("multipart/form-data; boundary=")
    assert "application/json" not in request.headers["content-type"]
    assert request.headers["authorization"].startswith("Bearer ")
    parts = _parts(request)
    assert set(parts) == {"dispute_id", "name", "file"}
    assert parts["dispute_id"][1] == b"dsp-1"
    assert parts["name"][1] == b"Signed lease page 3"
    assert parts["file"] == ("evidence.pdf", data, "application/pdf")
    assert (receipt.external_id, receipt.dispute_external_id) == ("file-77", "dsp-1")


@pytest.mark.parametrize(
    ("content_type", "extension"),
    [("image/jpeg", "jpeg"), ("image/png", "png"), ("image/tiff", "tiff")],
)
def test_each_image_type_is_sent_with_its_extension(content_type: str, extension: str) -> None:
    rig = make_rig(always(200, {"id": "file-1"}))
    StaxProvider(rig.transport).upload_dispute_evidence("dsp-1", "Receipt", b"x" * 10, content_type)
    assert _parts(rig.recorder.requests[0])["file"][0] == f"evidence.{extension}"


@pytest.mark.parametrize(
    ("name", "data", "content_type"),
    [
        ("Receipt", b"x" * EVIDENCE_MAX_BYTES, "application/pdf"),
        ("Receipt", b"x" * (EVIDENCE_MAX_BYTES + 1), "application/pdf"),
        ("Receipt", b"", "application/pdf"),
        ("Receipt", b"x", DOCX_TYPE),
        ("Receipt", b"x", "image/heic"),
        ("", b"x", "application/pdf"),
        ("Line\nbreak", b"x", "application/pdf"),
        ("Card 4111 1111 1111 1111", b"x", "application/pdf"),
        ("n" * 151, b"x", "application/pdf"),
    ],
    ids=[
        "10-MB",
        "10-MB-plus-1",
        "empty",
        "docx",
        "heic",
        "no-name",
        "line-break",
        "card-in-name",
        "long-name",
    ],
)
def test_upload_is_refused_before_any_request(name: str, data: bytes, content_type: str) -> None:
    rig = make_rig(always(200, {"id": "file-1"}))
    with pytest.raises(ValidationFailed):
        StaxProvider(rig.transport).upload_dispute_evidence("dsp-1", name, data, content_type)
    assert rig.recorder.requests == []


def test_upload_just_under_ten_mb_is_sent() -> None:
    rig = make_rig(always(200, {"id": "file-1"}))
    data = b"x" * (EVIDENCE_MAX_BYTES - 1)
    StaxProvider(rig.transport).upload_dispute_evidence("dsp-1", "Receipt", data, "image/png")
    assert _parts(rig.recorder.requests[0])["file"][1] == data


def test_a_400_past_respond_by_keeps_the_provider_sentence_and_is_not_retried(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sentence = "The respond_by date has passed for this dispute."
    rig = make_rig(always(400, {"message": sentence, "dispute_id": ["invalid"]}))
    with pytest.raises(ProviderRejected) as raised:
        StaxProvider(rig.transport).upload_dispute_evidence(
            "dsp-1", "Receipt", b"%PDF-1.4", "application/pdf"
        )
    assert (raised.value.status, raised.value.reason) == (400, sentence)
    assert sentence not in str(raised.value) and sentence not in caplog.text
    assert raised.value.error_fields == ("dispute_id", "message")
    assert len(rig.recorder.requests) == 1


@pytest.mark.parametrize(
    "body",
    [
        {"message": "The name field is required."},
        {"message": "Respond by 4111 1111 1111 1111 has passed."},
        {"message": "x" * 300 + " respond by date passed"},
        ["respond_by passed"],
        "not json",
    ],
    ids=["other-400", "card-number", "too-long", "list", "non-json"],
)
def test_any_other_400_carries_no_reason(body: object) -> None:
    content = body.encode() if isinstance(body, str) else json.dumps(body).encode()
    rig = make_rig(sequence([httpx.Response(400, content=content)]))
    with pytest.raises(ProviderRejected) as raised:
        StaxProvider(rig.transport).upload_dispute_evidence(
            "dsp-1", "Receipt", b"%PDF-1.4", "application/pdf"
        )
    # A top-level list is read too; only the safe, short, matching sentence is kept.
    assert raised.value.reason == ("respond_by passed" if isinstance(body, list) else "")


@pytest.mark.parametrize(
    "reply", [httpx.Response(503), httpx.ReadTimeout("slow")], ids=["503", "timeout"]
)
def test_upload_is_not_retried_after_a_5xx_or_a_timeout(reply: httpx.Response | Exception) -> None:
    rig = make_rig(sequence([reply]))
    with pytest.raises(ProviderUnavailable):
        StaxProvider(rig.transport).upload_dispute_evidence(
            "dsp-1", "Receipt", b"%PDF-1.4", "application/pdf"
        )
    assert len(rig.recorder.requests) == 1 and rig.clock.sleeps == []
    assert rig.budget.counts()[0] == 1


def test_upload_logs_the_dispute_and_file_ids_never_the_name_or_bytes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("INFO", logger="payments.stax.transport")
    rig = make_rig(always(200, {"id": "file-77"}))
    StaxProvider(rig.transport).upload_dispute_evidence(
        "dsp-1", "Signed lease page", b"%PDF-1.4 SECRET-BYTES", "application/pdf"
    )
    assert "path=/file/dispute status=200" in caplog.text
    assert "object_id=file-77" in caplog.text
    assert "Signed lease page" not in caplog.text and "SECRET-BYTES" not in caplog.text


# --- Sync: the fake provider through the service -----------------------------------------


def test_sync_creates_rows_links_our_payment_and_keeps_the_status_verbatim(
    fake: FakeProvider,
) -> None:
    world = a_paid_world(fake)
    a_dispute(fake, status="PREARBITRATION")
    a_dispute(fake, "dsp-2", transaction="txn-not-ours", status="Some New_Status")
    result = synced(fake)
    assert (result.read, result.created, result.changed, result.linked) == (2, 2, 0, 1)
    ours, other = the_dispute(), the_dispute("dsp-2")
    payment = Payment.objects.get(rent_charge=world.charge)
    assert (ours.payment_id, ours.operator_id, ours.store_id) == (
        payment.pk,
        world.operator.pk,
        world.lease.store_id,
    )
    assert (ours.status, ours.reason, ours.amount) == ("PREARBITRATION", "FRAUD", D("1500.00"))
    assert (other.status, other.payment_id, other.operator_id) == ("Some New_Status", None, None)
    assert ours.raw_summary["transaction"] == MASKED
    assert TXN not in json.dumps(ours.raw_summary)
    assert SyncState.objects.get(job_name=disputes.SYNC_JOB).last_success_at == NOW
    [audit] = AuditLog.objects.filter(action=disputes.ACTION_SYNCED)
    assert audit.after == {"read": 2, "created": 2, "changed": 0, "linked": 1, "more_pages": False}
    # Nothing about the charge itself changes.
    world.charge.refresh_from_db()
    assert world.charge.status == RentChargeStatus.PAID


def test_sync_reads_every_page_and_updates_without_duplicates(fake: FakeProvider) -> None:
    for number in range(5):  # the fake answers two a page
        a_dispute(fake, f"dsp-{number}", transaction=f"txn-{number}")
    assert synced(fake).read == 5
    pages = [args[0].page for name, args in fake.calls if name == "list_disputes"]  # type: ignore[attr-defined]
    assert pages == [1, 2, 3, 4]
    fake.disputes["dsp-3"] = replace(fake.disputes["dsp-3"], raw_status="WON")
    again = synced(fake)
    assert (again.created, again.changed) == (0, 1)
    assert Dispute.objects.count() == 5 and the_dispute("dsp-3").status == "WON"


def test_the_window_is_the_last_ninety_days(fake: FakeProvider) -> None:
    synced(fake)
    [query] = [args[0] for name, args in fake.calls if name == "list_disputes"]
    assert query.date_range == DateRange(TODAY - timedelta(days=90), TODAY)  # type: ignore[attr-defined]


def test_the_provider_down_keeps_the_last_fetch_and_its_time(fake: FakeProvider) -> None:
    a_dispute(fake)
    synced(fake)
    fake.disputes["dsp-1"] = replace(fake.disputes["dsp-1"], raw_status="WON")
    fake.fail_next("list_disputes", ProviderUnavailable(503))
    result = disputes.sync(now=NOW + timedelta(hours=1))
    assert (result.ok, result.message) == (
        False,
        disputes.SYNC_FAILED.format(error="ProviderUnavailable"),
    )
    assert the_dispute().status == "OPEN"
    assert disputes.last_synced() == NOW
    assert AuditLog.objects.filter(
        action=disputes.ACTION_SYNC_FAILED, after={"error": "ProviderUnavailable"}
    ).exists()
    page = client_for(Role.BILLING)[0].get(PAGE).content.decode()
    assert f"Last updated from {provider_name()} 2026-10-06 12:00." in page and ">OPEN<" in page


# --- Queueing the sync: Refresh, the daily tick, the webhooks ----------------------------


def test_refresh_queues_one_sync_and_is_busy_while_it_waits(fake: FakeProvider) -> None:
    client, user = client_for(Role.BILLING)
    response = client.post("/disputes/refresh/")
    assert response.status_code == 302
    [job] = Job.objects.filter(kind=JobKind.DISPUTES_SYNC)
    assert (job.entity_type, job.requested_by, job.params) == (
        "disputes",
        user,
        {"trigger": "manual"},
    )
    assert disputes.refresh(user) == disputes.Outcome(False, disputes.BUSY)
    assert Job.objects.filter(kind=JobKind.DISPUTES_SYNC).count() == 1
    assert "This page refreshes every 5 seconds." in client.get(PAGE).content.decode()
    [done] = run_queued_jobs()
    assert done.status == JobStatus.DONE
    assert disputes.refresh(user).ok


def test_a_viewer_cannot_refresh(fake: FakeProvider) -> None:
    client, user = client_for(Role.VIEWER)
    assert client.post("/disputes/refresh/").status_code == 403
    assert disputes.refresh(user).forbidden
    assert not Job.objects.exists()


def test_the_daily_tick_queues_the_sync_once_a_day(fake: FakeProvider) -> None:
    out = StringIO()
    call_command("collections_tick", stdout=out)
    assert "Dispute list read queued: yes." in out.getvalue()
    out = StringIO()
    call_command("collections_tick", stdout=out)
    assert "Dispute list read queued: no." in out.getvalue()
    [job] = Job.objects.filter(kind=JobKind.DISPUTES_SYNC)
    assert (job.params, job.requested_by) == ({"trigger": "daily"}, None)


def test_a_dispute_webhook_flags_the_payment_and_queues_the_sync(fake: FakeProvider) -> None:
    world = a_paid_world(fake)
    a_dispute(fake, status="INQUIRY")
    assert deliver("create_dispute", id=TXN) == "processed"
    world.charge.refresh_from_db()
    assert world.charge.needs_review
    [job] = Job.objects.filter(kind=JobKind.DISPUTES_SYNC)
    assert job.params == {"trigger": "webhook"}
    deliver("update_dispute", id=TXN)  # one sync waits already: nothing more queued
    assert Job.objects.filter(kind=JobKind.DISPUTES_SYNC).count() == 1
    run_queued_jobs()
    assert the_dispute().status == "INQUIRY"


# --- The page --------------------------------------------------------------------------------


@pytest.mark.parametrize(("role", "status"), [(Role.VIEWER, 403), (Role.BILLING, 200)])
def test_the_page_and_its_export_are_billing_and_above(
    fake: FakeProvider, role: Role, status: int
) -> None:
    client = client_for(role)[0]
    assert client.get(PAGE).status_code == status
    assert client.get("/disputes/export.csv").status_code == status


def test_the_empty_page_says_so_and_the_nav_links_it(fake: FakeProvider) -> None:
    page = client_for(Role.BILLING)[0].get(PAGE).content.decode()
    assert 'id="no-disputes"' in page and "Not updated yet." in page
    assert 'id="nav-disputes"' in page


def test_the_list_is_by_respond_by_with_days_left_highlighted_under_seven(
    fake: FakeProvider,
) -> None:
    cases = {
        "dsp-8": NOW + timedelta(days=8),
        "dsp-6": NOW + timedelta(days=6),
        "dsp-today": NOW + timedelta(hours=6),
        "dsp-past": NOW - timedelta(hours=1),
    }
    for external_id, respond_by in cases.items():
        a_dispute(fake, external_id, respond_by=respond_by, transaction=f"t-{external_id}")
    synced(fake)
    page = client_for(Role.BILLING)[0].get(PAGE).content.decode()
    rows = {name: the_dispute(name).pk for name in cases}
    order = [page.index(f'id="dispute-{rows[name]}"') for name in cases]
    assert order == sorted(order, reverse=True)  # past, today, 6 days, 8 days

    def row(name: str) -> str:
        start = page.index(f'id="dispute-{rows[name]}"')
        return page[page.rindex("<tr", 0, start) : page.index("</tr>", start)]

    assert 'class="dispute-past"' in row("dsp-past") and "<strong>Past</strong>" in row("dsp-past")
    assert 'class="dispute-soon"' in row("dsp-today") and "<strong>0</strong>" in row("dsp-today")
    assert 'class="dispute-soon"' in row("dsp-6") and "<strong>6</strong>" in row("dsp-6")
    assert 'class=""' in row("dsp-8") and ">8</td>" in row("dsp-8")


def test_the_page_and_csv_show_the_transaction_masked_only(fake: FakeProvider) -> None:
    long_id = "txn-abcdef123456"
    a_dispute(fake, "dsp-long", transaction=long_id)
    ready(fake)
    client = client_for(Role.ADMIN)[0]
    page = client.get(PAGE).content.decode()
    response = client.get("/disputes/export.csv")
    export = b"".join(response.streaming_content).decode()  # type: ignore[attr-defined]
    for text in (page, export):
        assert TXN not in text and "dsp-1" not in text
        assert long_id not in text and "dsp-long" not in text
        assert MASKED in text and "txn-…3456" in text
    header = export.splitlines()[0]
    assert header.startswith("Respond by,Days left,Status,Reason,Amount (USD),Operator,Store")
    dispute = the_dispute()
    assert f'href="/payments/{dispute.payment_id}/"' in page
    assert f'href="/operators/{dispute.operator_id}/"' in page


def test_billing_sees_no_evidence_form_and_cannot_post_one(fake: FakeProvider) -> None:
    dispute = ready(fake)
    client = client_for(Role.BILLING)[0]
    assert "Add evidence" not in client.get(PAGE).content.decode()
    response = client.post(
        f"/disputes/{dispute.pk}/evidence/",
        {"name": "Receipt", "file": a_file("receipt.pdf", make_pdf([["Receipt"]]))},
    )
    assert response.status_code == 403
    assert not DisputeEvidence.objects.exists() and not Document.objects.exists()


# --- Evidence ----------------------------------------------------------------------------


def test_evidence_through_the_page_the_worker_and_the_status_re_fetch(
    fake: FakeProvider, storage: MemoryDocumentStorage
) -> None:
    dispute = ready(fake)
    client, user = client_for(Role.ADMIN)
    pdf = make_pdf([["Lease page 3, signed by the operator."]])
    response = client.post(
        f"/disputes/{dispute.pk}/evidence/",
        {"name": "Signed lease page 3", "file": a_file("lease.pdf", pdf)},
    )
    assert response.status_code == 302
    [evidence] = DisputeEvidence.objects.all()
    document = evidence.document
    assert (document.operator_id, document.kind, document.store_id, document.lease_id) == (
        dispute.operator_id,
        DocumentKind.DISPUTE_EVIDENCE,
        None,
        None,
    )
    assert evidence.status == DisputeEvidenceStatus.QUEUED and fake.evidence == []
    [job] = Job.objects.filter(kind=JobKind.DISPUTE_EVIDENCE_UPLOAD)
    assert (job.entity_type, job.entity_id, job.requested_by) == (
        "dispute_evidence",
        evidence.pk,
        user,
    )

    done = run_queued_jobs()
    assert [(item.kind, item.status) for item in done] == [
        (JobKind.DISPUTE_EVIDENCE_UPLOAD, JobStatus.DONE),
        (JobKind.DISPUTES_SYNC, JobStatus.DONE),
    ]
    assert fake.evidence == [("dsp-1", "Signed lease page 3", pdf, "application/pdf")]
    evidence.refresh_from_db()
    assert (evidence.status, evidence.sent_at) == (DisputeEvidenceStatus.SENT, NOW)
    assert ExternalRef.objects.filter(
        entity_type="dispute_evidence", entity_id=evidence.pk, external_kind="dispute_file"
    ).exists()
    assert the_dispute().status == "EVIDENCE_UPLOADED"
    page = client.get(PAGE).content.decode()
    assert ">EVIDENCE_UPLOADED<" in page and "Signed lease page 3" in page
    actions = set(AuditLog.objects.values_list("action", flat=True))
    assert {
        "document.uploaded",
        "document.downloaded",
        disputes.ACTION_EVIDENCE_ADDED,
        disputes.ACTION_EVIDENCE_SENT,
    } <= actions


@pytest.mark.parametrize(
    ("size", "ok"),
    [(EVIDENCE_MAX_BYTES - 1, True), (EVIDENCE_MAX_BYTES, False), (EVIDENCE_MAX_BYTES + 1, False)],
    ids=["10-MB-minus-1", "10-MB", "10-MB-plus-1"],
)
def test_evidence_must_be_smaller_than_ten_mb(fake: FakeProvider, size: int, ok: bool) -> None:
    dispute = ready(fake)
    outcome = add(dispute, "receipt.png", png_of(size))
    assert outcome.ok is ok, outcome.message
    if not ok:
        assert outcome.message == disputes.TOO_LARGE
        assert not Document.objects.exists() and not DisputeEvidence.objects.exists()
    else:
        run_queued_jobs()
        assert len(fake.evidence[0][2]) == size


def test_a_tiff_is_accepted_and_a_docx_refused(fake: FakeProvider) -> None:
    dispute = ready(fake)
    assert add(dispute, "receipt.tiff", TIFF).ok
    run_queued_jobs()
    assert fake.evidence[0][3] == "image/tiff"
    docx = make_docx(part(paragraph("Receipt")))
    outcome = add(dispute, "receipt.docx", docx)
    assert (outcome.ok, outcome.message) == (
        False,
        "Dispute evidence must be a PDF, or a JPEG, PNG or TIFF image.",
    )
    assert Document.objects.count() == 1


def test_a_tiff_is_refused_on_a_store_or_lease(fake: FakeProvider) -> None:
    from ledger.services import documents

    world = make_world(fake)
    outcome = documents.upload(world.lease, "other", a_file("scan.tiff", TIFF), "", admin())
    assert (outcome.ok, outcome.code) == (False, documents.TYPE_NOT_ACCEPTED)
    on_store = documents.upload(
        world.lease.store, DocumentKind.DISPUTE_EVIDENCE, a_file("x.pdf", b"%PDF-1.4"), "", admin()
    )
    assert (on_store.ok, on_store.code) == (False, documents.KIND_UNKNOWN)


def test_a_pdf_with_a_routing_and_account_pair_is_refused_before_any_provider_call(
    fake: FakeProvider,
) -> None:
    dispute = ready(fake)
    calls = len(fake.calls)
    bank = make_pdf([[f"Routing {ROUTING} account {ACCOUNT}"]])
    outcome = add(dispute, "statement.pdf", bank, acknowledged=True)
    assert not outcome.ok
    assert "card or account number" in outcome.message
    assert not Document.objects.exists() and not DisputeEvidence.objects.exists()
    assert not Job.objects.filter(kind=JobKind.DISPUTE_EVIDENCE_UPLOAD).exists()
    run_queued_jobs()
    assert len(fake.calls) == calls and fake.evidence == []


@pytest.mark.parametrize(("name", "data"), [("receipt.png", PNG), ("receipt.jpg", JPEG)])
def test_an_image_without_the_notice_is_refused(fake: FakeProvider, name: str, data: bytes) -> None:
    dispute = ready(fake)
    outcome = add(dispute, name, data, acknowledged=False)
    assert (outcome.ok, outcome.message) == (False, disputes.NOTICE_REQUIRED)
    assert not Document.objects.exists()
    assert AuditLog.objects.filter(
        action=disputes.ACTION_EVIDENCE_REFUSED, after={"reason": "notice_required"}
    ).exists()
    # A PDF needs no notice: its text is checked.
    assert add(dispute, "receipt.pdf", make_pdf([["Receipt"]]), acknowledged=False).ok


@pytest.mark.parametrize(
    ("respond_by", "ok"),
    [
        (NOW + timedelta(hours=6), True),
        (NOW + timedelta(days=6), True),
        (NOW + timedelta(days=8), True),
        (NOW - timedelta(minutes=1), False),
    ],
    ids=["today", "in-6-days", "in-8-days", "past"],
)
def test_evidence_after_the_respond_by_time_is_refused(
    fake: FakeProvider, respond_by: datetime, ok: bool
) -> None:
    dispute = ready(fake, respond_by=respond_by)
    outcome = add(dispute, "receipt.pdf", make_pdf([["Receipt"]]))
    assert outcome.ok is ok
    if not ok:
        assert outcome.message == disputes.PAST_RESPOND_BY and not Document.objects.exists()
        page = client_for(Role.ADMIN)[0].get(PAGE).content.decode()
        assert "Add evidence" not in page and "no longer takes evidence" in page


def test_the_provider_400_past_respond_by_is_shown_and_the_row_failed(fake: FakeProvider) -> None:
    dispute = ready(fake)
    assert add(dispute, "receipt.pdf", make_pdf([["Receipt"]])).ok
    # The provider's clock says the date passed before ours does.
    fake.disputes["dsp-1"] = replace(fake.disputes["dsp-1"], respond_by=NOW - timedelta(hours=1))
    [job] = run_queued_jobs()
    assert (job.status, job.result) == (JobStatus.FAILED, RESPOND_BY_PASSED)
    [evidence] = DisputeEvidence.objects.all()
    assert (evidence.status, evidence.error) == (DisputeEvidenceStatus.FAILED, "ProviderRejected")
    page = client_for(Role.ADMIN)[0].get(PAGE).content.decode()
    assert RESPOND_BY_PASSED in page and "Send again" in page


def test_a_failed_upload_is_recorded_by_class_name_and_sent_again_by_a_new_job(
    fake: FakeProvider,
) -> None:
    dispute = ready(fake)
    assert add(dispute, "receipt.pdf", make_pdf([["Receipt"]])).ok
    fake.fail_next("upload_dispute_evidence", ProviderUnavailable(503))
    [job] = run_queued_jobs()
    assert job.status == JobStatus.FAILED
    assert job.result == disputes.FAILED.format(error="ProviderUnavailable")
    [evidence] = DisputeEvidence.objects.all()
    assert (evidence.status, evidence.error) == (
        DisputeEvidenceStatus.FAILED,
        "ProviderUnavailable",
    )
    billing = client_for(Role.BILLING)[0]
    assert billing.post(f"/disputes/evidence/{evidence.pk}/retry/").status_code == 403
    client = client_for(Role.ADMIN)[0]
    assert client.post(f"/disputes/evidence/{evidence.pk}/retry/").status_code == 302
    assert Job.objects.filter(kind=JobKind.DISPUTE_EVIDENCE_UPLOAD).count() == 2
    run_queued_jobs()
    evidence.refresh_from_db()
    assert evidence.status == DisputeEvidenceStatus.SENT and len(fake.evidence) == 1
    again = disputes.retry_evidence(evidence, admin())
    assert (again.ok, again.message) == (False, disputes.NOT_FAILED)


def test_stored_bytes_that_changed_are_never_sent(
    fake: FakeProvider, storage: MemoryDocumentStorage
) -> None:
    dispute = ready(fake)
    assert add(dispute, "receipt.pdf", make_pdf([["Receipt"]])).ok
    [document] = Document.objects.all()
    data, content_type = storage.blobs[document.blob_path]
    storage.blobs[document.blob_path] = (data[:-1] + b"X", content_type)
    [job] = run_queued_jobs()
    assert job.status == JobStatus.FAILED and fake.evidence == []
    assert DisputeEvidence.objects.get().error == "StorageUnavailable"


def test_a_dispute_without_our_operator_takes_no_evidence(fake: FakeProvider) -> None:
    a_dispute(fake, transaction="txn-not-ours")
    synced(fake)
    outcome = add(the_dispute(), "receipt.pdf", make_pdf([["Receipt"]]))
    assert (outcome.ok, outcome.message) == (False, disputes.NO_OPERATOR)
    assert not Document.objects.exists()


@pytest.mark.parametrize("role", [Role.VIEWER, Role.BILLING])
def test_only_an_admin_adds_evidence(fake: FakeProvider, role: Role) -> None:
    dispute = ready(fake)
    outcome = add(dispute, "receipt.pdf", make_pdf([["Receipt"]]), actor=client_for(role)[1])
    assert outcome.forbidden and not Document.objects.exists()


@pytest.mark.parametrize(
    ("name", "message"),
    [
        ("   ", disputes.NAME_REQUIRED),
        ("n" * 151, disputes.NAME_TOO_LONG),
        ("Card 4111 1111 1111 1111", "This text may not contain card or account numbers."),
    ],
    ids=["blank", "long", "card"],
)
def test_a_bad_name_is_refused(fake: FakeProvider, name: str, message: str) -> None:
    dispute = ready(fake)
    outcome = disputes.add_evidence(
        dispute, a_file("receipt.pdf", make_pdf([["Receipt"]])), name, admin()
    )
    assert (outcome.ok, outcome.message) == (False, message)
    assert not Document.objects.exists()


def test_the_page_never_shows_a_provider_file_id_or_an_unmasked_transaction(
    fake: FakeProvider,
) -> None:
    dispute = ready(fake)
    assert add(dispute, "receipt.pdf", make_pdf([["Receipt"]])).ok
    run_queued_jobs()
    file_id = ExternalRef.objects.get(external_kind="dispute_file").external_id
    page = client_for(Role.ADMIN)[0].get(PAGE).content.decode()
    assert file_id not in page and TXN not in page
    assert MASKED in page


def test_the_operator_keeps_dispute_evidence_only(fake: FakeProvider) -> None:
    operator = Operator.objects.create(display_name="Lone Operator")
    from ledger.services import documents

    outcome = documents.upload(operator, "other", a_file("x.pdf", make_pdf([["x"]])), "", admin())
    assert (outcome.ok, outcome.code) == (False, documents.KIND_UNKNOWN)
    assert uuid.UUID(str(operator.pk))

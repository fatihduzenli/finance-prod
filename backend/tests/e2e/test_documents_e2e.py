"""Documents in a browser (docs/qa/PLAN-010c D7): upload from the store page with the file
input and by dropping a file on the box, then preview it in the page's frame.

The live server runs in this process, so the in-memory storage replaces
``documents_storage.get_storage``: nothing leaves the machine.
"""

from __future__ import annotations

import base64
import time

import pytest
from playwright.sync_api import BrowserType, Page
from pytest_django.live_server_helper import LiveServer

import documents_storage
from accounts.roles import Role
from documents_storage.memory import MemoryDocumentStorage
from ledger.models import Document
from tests.auth_helpers import TEST_PASSWORD, current_code, make_user
from tests.document_files import JPEG, PNG, make_pdf
from tests.test_ledger_models import new_store, saved

#: The page Chromium's built-in PDF viewer runs in.
PDF_VIEWER_URL = "chrome-extension://mhjfbmdgcfjbbpaeojofohoefgiehjai/index.html"

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=True)]


def _sign_in(page: Page, live_server: LiveServer, username: str) -> None:
    _, device = make_user(username, Role.BILLING)
    assert device is not None
    page.goto(f"{live_server.url}/account/login/")
    page.fill("#id_auth-username", username)
    page.fill("#id_auth-password", TEST_PASSWORD)
    page.get_by_role("button", name="Sign in").click()
    token_field = page.locator("#id_token-otp_token")
    token_field.wait_for(state="visible")
    token_field.fill(current_code(device))
    page.get_by_role("button", name="Next").click()
    page.wait_for_url(f"{live_server.url}/")  # the dashboard (PLAN-011 D1)


def test_upload_by_input_and_by_drop_then_preview(
    live_server: LiveServer, page: Page, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = MemoryDocumentStorage()
    monkeypatch.setattr(documents_storage, "get_storage", lambda: storage)
    store = saved(new_store())
    _sign_in(page, live_server, "e2e-documents")
    store_page = f"{live_server.url}/stores/{store.pk}/"

    page.goto(f"{store_page}?tab=documents")
    page.locator("#id_file").set_input_files(
        {"name": "plan.png", "mimeType": "image/png", "buffer": PNG}
    )
    page.select_option("#id_kind", "floor_plan")
    page.locator("#upload-document").click()
    page.wait_for_url(f"{store_page}?tab=documents#tab-documents")
    assert "Document uploaded." in page.content()
    assert page.locator("#documents-list").inner_text().count("plan.png") == 1

    # Drop one file on the box: the script moves it into the input; the form posts it.
    hint = page.locator(".js-document-drop-hint")
    assert hint.is_visible()
    page.evaluate(
        """(encoded) => {
            const bytes = Uint8Array.from(atob(encoded), (c) => c.charCodeAt(0));
            const transfer = new DataTransfer();
            transfer.items.add(new File([bytes], "front.jpg", {type: "image/jpeg"}));
            const zone = document.querySelector(".js-document-drop");
            zone.dispatchEvent(new DragEvent("drop", {dataTransfer: transfer, bubbles: true,
                                                      cancelable: true}));
        }""",
        base64.b64encode(JPEG).decode(),
    )
    assert hint.inner_text() == "Selected: front.jpg"
    assert page.evaluate("document.querySelector('#id_file').files.length") == 1
    page.locator("#upload-document").click()
    page.wait_for_url(f"{store_page}?tab=documents#tab-documents")
    assert Document.objects.count() == 2
    assert len(storage.blobs) == 2

    # Preview in the page's frame, served by the app with the sandbox policy.
    png = Document.objects.get(original_filename="plan.png")
    page.locator(f"#document-{png.pk} .document-preview").click()
    frame = page.frame_locator("#document-preview iframe")
    frame.locator("img").wait_for(state="attached")
    assert page.locator("#document-preview").inner_text().startswith("Preview of plan.png")


def test_a_pdf_lease_previews_in_the_frame(
    live_server: LiveServer, browser_type: BrowserType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 1 on Batch A: a PDF lease previews in the page's frame, served with the
    PDF preview's own policy (no ``sandbox``).

    The default headless shell has no PDF viewer (a PDF in a frame is not shown at all),
    so this test launches the full Chromium build that ``playwright install chromium``
    installs beside it (channel ``chromium``, the new headless mode), which has one.
    That build renders the PDF under ``sandbox`` as well, so this test proves the
    headers and that the viewer loads with them; it cannot show the blank frame that
    ``sandbox`` caused in the owner's browser.
    """
    storage = MemoryDocumentStorage()
    monkeypatch.setattr(documents_storage, "get_storage", lambda: storage)
    store = saved(new_store())
    browser = browser_type.launch(channel="chromium")
    try:
        page = browser.new_page()
        _sign_in(page, live_server, "e2e-documents-pdf")
        store_page = f"{live_server.url}/stores/{store.pk}/"
        page.goto(f"{store_page}?tab=documents")
        page.locator("#id_file").set_input_files(
            {"name": "lease.pdf", "mimeType": "application/pdf", "buffer": make_pdf([["Lease"]])}
        )
        page.select_option("#id_kind", "lease_agreement")
        page.locator("#upload-document").click()
        page.wait_for_url(f"{store_page}?tab=documents#tab-documents")
        pdf = Document.objects.get(original_filename="lease.pdf")
        preview_url = f"{live_server.url}/documents/{pdf.pk}/preview/"

        with page.expect_response(preview_url) as preview:
            page.locator(f"#document-{pdf.pk} .document-preview").click()
        response = preview.value
        assert response.status == 200
        headers = response.all_headers()
        assert headers["content-type"] == "application/pdf"
        assert headers["content-disposition"] == 'inline; filename="lease.pdf"'
        assert headers["content-security-policy"] == "default-src 'none'; frame-ancestors 'self'"
        assert headers["x-content-type-options"] == "nosniff"
        assert headers["x-frame-options"] == "SAMEORIGIN"

        # The viewer loaded: the frame's document is the PDF, and Chromium has opened its
        # PDF viewer inside it (a child frame of the viewer's extension page).
        page.wait_for_function(
            "url => [...document.querySelectorAll('#document-preview iframe')]"
            ".some(f => f.src === url)",
            arg=preview_url,
        )
        frame = next(frame for frame in page.frames if frame.url == preview_url)
        assert frame.evaluate("document.contentType") == "application/pdf"
        deadline = time.monotonic() + 15
        while not [child for child in frame.child_frames if child.url == PDF_VIEWER_URL]:
            assert time.monotonic() < deadline, "the PDF viewer did not load in the frame"
            page.wait_for_timeout(100)
    finally:
        browser.close()

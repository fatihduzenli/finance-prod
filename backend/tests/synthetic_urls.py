"""A URL map for role tests: the real one plus stand-ins for routes that come later.

Refund, void, settings and invoice views do not exist yet (PLAN-003 §4). These
stand-ins let the billing-vs-admin rules be exercised through the real middleware now;
the tests move to the real routes when Steps 4+ add them.
"""

from __future__ import annotations

from django.http import HttpRequest, HttpResponse
from django.urls import include, path

from accounts import views as account_views
from finance.urls import urlpatterns as real_urlpatterns


def _ok(request: HttpRequest) -> HttpResponse:
    return HttpResponse("done")


synthetic = (
    [
        path("refund/", _ok, name="refund"),
        path("settings/", _ok, name="settings"),
        path("invoice/", _ok, name="invoice"),
    ],
    "synthetic",
)

# The real sign-out-everywhere view mounted under another namespace: its rule is keyed
# on "accounts:sign_out_everywhere", so it must not apply here.
other = (
    [path("sign-out-everywhere/", account_views.sign_out_everywhere, name="sign_out_everywhere")],
    "other",
)

urlpatterns = [
    *real_urlpatterns,
    path("synthetic/", include(synthetic)),
    path("other/", include(other)),
]

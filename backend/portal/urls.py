"""The portal's routes, mounted at ``/portal/`` (PLAN-014 H1). Every one is listed in
``accounts.registry.PORTAL_ROUTE_NAMES``; the public ones in ``PORTAL_PUBLIC_ROUTE_NAMES``. The
last, ``portal:not_found``, answers any other path under ``/portal/`` with the portal's 404."""

from __future__ import annotations

from django.urls import path, re_path

from portal import views

app_name = "portal"

urlpatterns = [
    path("", views.home, name="home"),
    path("login/", views.login, name="login"),
    path("logout/", views.logout, name="logout"),
    path("invite/<str:token>/", views.invite, name="invite"),
    path("reset/", views.reset_request, name="reset_request"),
    path("reset/<str:token>/", views.reset_confirm, name="reset_confirm"),
    path("stores/<str:pk>/", views.store, name="store"),
    path("stores/<str:pk>/invoices/", views.invoices, name="invoices"),
    path("stores/<str:pk>/payments/", views.payments, name="payments"),
    path("stores/<str:pk>/pay/", views.pay, name="pay"),
    path("stores/<str:pk>/bank/", views.bank, name="bank"),
    path("statement/", views.statement, name="statement"),
    path("account/", views.account, name="account"),
    path("account/password/", views.password, name="password"),
    # Last: any other path under /portal/ is the portal's 404, never the staff 401.
    re_path(r"^.*$", views.not_found, name="not_found"),
]

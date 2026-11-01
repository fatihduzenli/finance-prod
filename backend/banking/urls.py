"""The Bank deposits routes, mounted at ``/admin-settings/bank-deposits/`` (PLAN-014 I2, I4).
Every one is classified in ``accounts.registry``: the page (and the Choose and Split pages'
GET) read-only for billing, the recording actions billing POST, the link actions admin POST;
each view checks the role again."""

from __future__ import annotations

from django.urls import path

from banking import views

app_name = "banking"

urlpatterns = [
    path("", views.page, name="page"),
    path("fetch-now/", views.fetch_now, name="fetch_now"),
    path("link-token/", views.link_token, name="link_token"),
    path("link-exchange/", views.link_exchange, name="link_exchange"),
    path("disconnect/", views.disconnect, name="disconnect"),
    path("<str:pk>/confirm/", views.confirm, name="confirm"),
    path("<str:pk>/choose/", views.choose, name="choose"),
    path("<str:pk>/split/", views.split, name="split"),
    path("<str:pk>/ignore/", views.ignore, name="ignore"),
    path("<str:pk>/undo/", views.undo, name="undo"),
]

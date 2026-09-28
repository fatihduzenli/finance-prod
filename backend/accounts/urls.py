"""Account routes. Every unsafe route here has a rule in accounts.registry."""

from __future__ import annotations

from django.urls import path

from accounts import views

app_name = "accounts"

urlpatterns = [
    path("sign-out-everywhere/", views.sign_out_everywhere, name="sign_out_everywhere"),
]

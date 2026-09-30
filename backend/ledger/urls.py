"""Ledger routes (docs/qa/PLAN-006-operators.md §4.1). Namespace ``ledger``.

Every unsafe route here has a rule in ``accounts.registry``; the read-only ones are
listed there too, so ``backend/tests/test_route_registry.py`` sees them classified.
There is no delete route: archiving is the only way to retire an operator.

``pk`` is matched as text, as the admin matches its object IDs, so every operator path
resolves to its route and the role registry decides on an unsafe request before the
view runs. The view then parses the UUID and loads the row, or answers 404.
"""

from __future__ import annotations

from django.urls import include, path

from ledger.views import operators

app_name = "ledger"

operator_patterns = (
    [
        path("", operators.operator_list, name="list"),
        path("new/", operators.operator_create, name="create"),
        path("<str:pk>/", operators.operator_detail, name="detail"),
        path("<str:pk>/edit/", operators.operator_edit, name="edit"),
        path("<str:pk>/archive/", operators.operator_archive, name="archive"),
        path("<str:pk>/retry-sync/", operators.operator_retry_sync, name="retry_sync"),
        path("<str:pk>/link/", operators.operator_link, name="link"),
    ],
    "operators",
)

urlpatterns = [
    path("operators/", include(operator_patterns)),
]

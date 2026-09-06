"""Shared visibility and eligibility rules for technical-only Asset fixtures."""
from __future__ import annotations

from django.db.models import Q, QuerySet

from studio.models import Asset


WORKFLOW_SMOKE_FIXTURE_SCOPE = "workflow_smoke"


def is_workflow_smoke_fixture_asset(asset: Asset) -> bool:
    metadata = asset.metadata if isinstance(asset.metadata, dict) else {}
    return (
        str(metadata.get("fixture_scope") or "").strip().casefold()
        == WORKFLOW_SMOKE_FIXTURE_SCOPE
    )


def exclude_workflow_smoke_fixture_assets(queryset: QuerySet) -> QuerySet:
    """Keep technical smoke fixtures out of normal production/library queries."""
    return queryset.filter(
        Q(metadata__fixture_scope__isnull=True)
        | ~Q(metadata__fixture_scope__iexact=WORKFLOW_SMOKE_FIXTURE_SCOPE),
    )

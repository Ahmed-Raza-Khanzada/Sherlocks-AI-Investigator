"""Public lookup service wrapper."""

from __future__ import annotations

from cdr_report_app.domain.provider_models import ProviderResult, SearchSubject
from cdr_report_app.integrations.providers import UnifiedLookupService
from cdr_report_app.settings import Settings


def lookup_subject(
    settings: Settings,
    subject: SearchSubject,
    provider_names: list[str] | None = None,
) -> dict[str, ProviderResult]:
    service = UnifiedLookupService(settings)
    return service.lookup_all(subject, provider_names=provider_names)


def lookup_single_provider(
    settings: Settings,
    provider_name: str,
    subject: SearchSubject,
) -> ProviderResult:
    service = UnifiedLookupService(settings)
    return service.lookup_one(provider_name, subject)

"""Base contracts for provider integrations."""

from __future__ import annotations

from abc import ABC, abstractmethod

from cdr_report_app.domain.provider_models import ProviderResult, SearchSubject


class PersonLookupProvider(ABC):
    provider_name: str

    @abstractmethod
    def lookup(self, subject: SearchSubject) -> ProviderResult:
        """Run a normalized lookup for a subject."""
        raise NotImplementedError


class AttachmentProvider(ABC):
    provider_name: str

    @abstractmethod
    def fetch_attachment(self, *args, **kwargs) -> ProviderResult:
        """Fetch a normalized attachment artifact."""
        raise NotImplementedError

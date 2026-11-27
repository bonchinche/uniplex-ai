from dataclasses import dataclass
from typing import Callable

from .provider_base import ImageAIProvider, ProviderError


ProviderFactory = Callable[[], ImageAIProvider]


@dataclass(frozen=True)
class ProviderRegistration:
    adapter_id: str
    factory: ProviderFactory


class ProviderRegistry:
    def __init__(self):
        self._providers: dict[str, ProviderRegistration] = {}

    def register(
        self,
        name: str,
        adapter_id: str,
        factory: ProviderFactory,
    ) -> None:
        normalized = name.strip().lower()

        if not normalized:
            raise ValueError("Provider name cannot be empty")

        if not adapter_id.strip():
            raise ValueError("Adapter ID cannot be empty")

        if normalized in self._providers:
            raise ValueError(
                f"Provider already registered: {normalized}"
            )

        self._providers[normalized] = ProviderRegistration(
            adapter_id=adapter_id,
            factory=factory,
        )

    def create(self, name: str) -> ImageAIProvider:
        registration = self._providers.get(name.strip().lower())

        if registration is None:
            raise ProviderError(
                code="provider_not_configured",
                message=f"Unknown AI provider: {name}",
                retryable=False,
            )

        provider = registration.factory()

        if not isinstance(provider, ImageAIProvider):
            raise TypeError("Factory returned invalid provider")

        return provider

    def adapter_id(self, name: str) -> str:
        registration = self._providers.get(name.strip().lower())

        if registration is None:
            raise ProviderError(
                code="provider_not_configured",
                message=f"Unknown AI provider: {name}",
                retryable=False,
            )

        return registration.adapter_id

    def available(self) -> tuple[str, ...]:
        return tuple(sorted(self._providers))

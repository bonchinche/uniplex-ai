from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ProviderCapabilities:
    provider_name: str
    supported_mime_types: tuple[str, ...]
    max_image_bytes: int
    supports_structured_output: bool


@dataclass
class ProviderRequest:
    request_id: str
    image_path: Path
    mime_type: str
    source_metadata: dict[str, Any]
    instructions: dict[str, Any]
    output_contract: dict[str, Any]
    generation: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProviderResponse:
    provider_name: str
    model_name: str
    model_version: str | None
    output: dict[str, Any]
    request_metadata: dict[str, Any] = field(default_factory=dict)
    usage: dict[str, Any] = field(default_factory=dict)
    latency_ms: int | None = None


class ProviderError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        retryable: bool = False,
        diagnostics: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.diagnostics = diagnostics or {}


class ImageAIProvider(ABC):
    @abstractmethod
    def capabilities(self) -> ProviderCapabilities:
        """Return provider capabilities."""

    @abstractmethod
    def analyze(self, request: ProviderRequest) -> ProviderResponse:
        """Analyze one image and return structured output."""

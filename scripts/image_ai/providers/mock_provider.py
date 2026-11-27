from pathlib import Path
from time import perf_counter

from ..provider_base import (
    ImageAIProvider,
    ProviderCapabilities,
    ProviderError,
    ProviderRequest,
    ProviderResponse,
)


class MockProvider(ImageAIProvider):
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider_name="mock",
            supported_mime_types=(
                "image/png",
                "image/jpeg",
                "image/webp",
            ),
            max_image_bytes=25 * 1024 * 1024,
            supports_structured_output=True,
        )

    def analyze(self, request: ProviderRequest) -> ProviderResponse:
        started = perf_counter()
        path = Path(request.image_path)

        if not path.is_file():
            raise ProviderError(
                code="image_not_found",
                message="Image file does not exist",
                retryable=False,
            )

        if request.mime_type not in self.capabilities().supported_mime_types:
            raise ProviderError(
                code="unsupported_mime_type",
                message="Mock provider does not support this image format",
                retryable=False,
            )

        if path.stat().st_size > self.capabilities().max_image_bytes:
            raise ProviderError(
                code="image_too_large",
                message="Image exceeds mock provider size limit",
                retryable=False,
            )

        output = {
            "contract_version": "1.0",
            "image_summary": (
                "TEST ONLY: image analysis was not performed"
            ),
            "detected_language": "unknown",
            "document_classification": {
                "category": "test_fixture",
                "confidence": 0,
                "alternatives": [],
            },
            "facts": [],
            "overall_review": {
                "recommendation": "needs_review",
                "reasons": ["mock_provider_no_real_analysis"],
            },
            "warnings": [
                "MOCK PROVIDER: this is not a real AI analysis"
            ],
        }

        latency_ms = int((perf_counter() - started) * 1000)

        return ProviderResponse(
            provider_name="mock",
            model_name="mock-provider",
            model_version="1.0",
            output=output,
            request_metadata={
                "mock": True,
                "request_id": request.request_id,
            },
            usage={},
            latency_ms=latency_ms,
        )

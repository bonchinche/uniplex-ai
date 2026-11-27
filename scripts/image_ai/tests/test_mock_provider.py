from pathlib import Path

import pytest

from image_ai.provider_base import ProviderError, ProviderRequest
from image_ai.providers.mock_provider import MockProvider


def make_request(path, mime_type="image/png"):
    return ProviderRequest(
        request_id="test-request-001",
        image_path=Path(path),
        mime_type=mime_type,
        source_metadata={},
        instructions={},
        output_contract={},
    )


def test_capabilities():
    provider = MockProvider()
    capabilities = provider.capabilities()

    assert capabilities.provider_name == "mock"
    assert "image/png" in capabilities.supported_mime_types
    assert "image/jpeg" in capabilities.supported_mime_types
    assert "image/webp" in capabilities.supported_mime_types
    assert capabilities.max_image_bytes == 25 * 1024 * 1024
    assert capabilities.supports_structured_output is True


def test_analyze_returns_mock_response(tmp_path):
    image = tmp_path / "test.png"
    image.write_bytes(b"test image")

    response = MockProvider().analyze(make_request(image))

    assert response.provider_name == "mock"
    assert response.model_name == "mock-provider"
    assert response.model_version == "1.0"
    assert response.output["contract_version"] == "1.0"
    assert response.output["facts"] == []
    assert response.output["overall_review"]["recommendation"] == "needs_review"
    assert response.request_metadata["mock"] is True
    assert response.request_metadata["request_id"] == "test-request-001"
    assert response.latency_ms is not None
    assert response.latency_ms >= 0


def test_mock_output_passes_real_output_contract(tmp_path):
    from image_ai.contracts import validate_output

    image = tmp_path / "test.png"
    image.write_bytes(b"test image")

    response = MockProvider().analyze(make_request(image))

    validate_output(
        response.output,
        "/opt/uniplex/config/image_analysis_contract_v1.json",
    )


def test_missing_image_is_rejected(tmp_path):
    image = tmp_path / "missing.png"

    with pytest.raises(ProviderError) as exc:
        MockProvider().analyze(make_request(image))

    assert exc.value.code == "image_not_found"
    assert exc.value.retryable is False


@pytest.mark.parametrize(
    "mime_type",
    ["application/pdf", "image/tiff", "image/bmp"],
)
def test_unsupported_mime_type_is_rejected(tmp_path, mime_type):
    image = tmp_path / "test.bin"
    image.write_bytes(b"test image")

    with pytest.raises(ProviderError) as exc:
        MockProvider().analyze(make_request(image, mime_type))

    assert exc.value.code == "unsupported_mime_type"
    assert exc.value.retryable is False


def test_oversized_image_is_rejected(tmp_path):
    image = tmp_path / "large.png"
    with image.open("wb") as file:
        file.truncate(25 * 1024 * 1024 + 1)

    provider = MockProvider()

    with pytest.raises(ProviderError) as exc:
        provider.analyze(make_request(image))

    assert exc.value.code == "image_too_large"
    assert exc.value.retryable is False

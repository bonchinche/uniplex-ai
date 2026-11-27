import pytest

from image_ai.provider_base import (
    ImageAIProvider,
    ProviderCapabilities,
    ProviderError,
)
from image_ai.provider_registry import ProviderRegistry


class DummyProvider(ImageAIProvider):
    def capabilities(self):
        return ProviderCapabilities(
            provider_name="dummy",
            supported_mime_types=("image/png",),
            max_image_bytes=1024,
            supports_structured_output=True,
        )

    def analyze(self, request):
        raise NotImplementedError


def make_registry():
    registry = ProviderRegistry()
    registry.register(
        "dummy",
        "test.DummyProvider",
        DummyProvider,
    )
    return registry


def test_register_and_create_provider():
    registry = make_registry()

    provider = registry.create("dummy")

    assert isinstance(provider, DummyProvider)
    assert provider.capabilities().provider_name == "dummy"


def test_provider_name_is_normalized():
    registry = make_registry()

    assert registry.create("DUMMY") is not None
    assert registry.adapter_id("Dummy") == "test.DummyProvider"


def test_duplicate_registration_is_rejected():
    registry = make_registry()

    with pytest.raises(ValueError, match="already registered"):
        registry.register("DUMMY", "another.Adapter", DummyProvider)


def test_unknown_provider_is_rejected():
    registry = ProviderRegistry()

    with pytest.raises(ProviderError) as exc:
        registry.create("unknown")

    assert exc.value.code == "provider_not_configured"
    assert exc.value.retryable is False


def test_invalid_factory_result_is_rejected():
    registry = ProviderRegistry()
    registry.register("invalid", "test.Invalid", lambda: object())

    with pytest.raises(TypeError, match="invalid provider"):
        registry.create("invalid")


def test_available_returns_sorted_names():
    registry = ProviderRegistry()
    registry.register("zeta", "test.Zeta", DummyProvider)
    registry.register("alpha", "test.Alpha", DummyProvider)

    assert registry.available() == ("alpha", "zeta")

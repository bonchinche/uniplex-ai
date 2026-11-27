import json

import pytest

from image_ai.config_loader import (
    AIConfigError,
    create_default_provider,
    load_config,
)
from image_ai.provider_base import (
    ImageAIProvider,
    ProviderCapabilities,
)
from image_ai.provider_registry import ProviderRegistry


class DummyProvider(ImageAIProvider):
    def capabilities(self):
        return ProviderCapabilities(
            provider_name="mock",
            supported_mime_types=("image/png",),
            max_image_bytes=1024,
            supports_structured_output=True,
        )

    def analyze(self, request):
        raise NotImplementedError


def valid_config():
    return {
        "config_version": "1.0",
        "default_provider": "mock",
        "providers": {
            "mock": {
                "enabled": True,
                "adapter": "test.MockProvider",
                "model": "test-model",
            }
        },
    }


def write_config(tmp_path, config):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


def make_registry():
    registry = ProviderRegistry()
    registry.register("mock", "test.MockProvider", DummyProvider)
    return registry


def test_load_valid_config(tmp_path):
    path = write_config(tmp_path, valid_config())

    config = load_config(path)

    assert config["default_provider"] == "mock"


def test_create_default_provider():
    provider = create_default_provider(valid_config(), make_registry())

    assert isinstance(provider, DummyProvider)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda c: c.update(config_version="2.0"), "Unsupported AI config version"),
        (lambda c: c.update(providers={}), "No providers configured"),
        (lambda c: c.update(default_provider=""), "Default provider is not configured"),
        (lambda c: c.update(default_provider="missing"), "Default provider does not exist"),
        (lambda c: c["providers"]["mock"].update(enabled=False), "Default provider is disabled"),
        (lambda c: c["providers"]["mock"].update(adapter=""), "Provider adapter is not configured"),
        (lambda c: c["providers"]["mock"].update(model=""), "Provider model is not configured"),
    ],
)
def test_invalid_config_is_rejected(tmp_path, change, message):
    config = valid_config()
    change(config)
    path = write_config(tmp_path, config)

    with pytest.raises(AIConfigError, match=message):
        load_config(path)


def test_missing_config_file_is_rejected(tmp_path):
    with pytest.raises(AIConfigError, match="Cannot read AI config"):
        load_config(tmp_path / "missing.json")


def test_adapter_mismatch_is_rejected():
    config = valid_config()
    registry = ProviderRegistry()
    registry.register("mock", "different.Adapter", DummyProvider)

    with pytest.raises(AIConfigError, match="does not match registry"):
        create_default_provider(config, registry)


def test_unregistered_provider_is_rejected():
    with pytest.raises(AIConfigError, match="Provider is not registered"):
        create_default_provider(valid_config(), ProviderRegistry())


def test_provider_name_mismatch_is_rejected():
    class WrongNameProvider(DummyProvider):
        def capabilities(self):
            return ProviderCapabilities(
                provider_name="wrong",
                supported_mime_types=("image/png",),
                max_image_bytes=1024,
                supports_structured_output=True,
            )

    registry = ProviderRegistry()
    registry.register("mock", "test.MockProvider", WrongNameProvider)

    with pytest.raises(AIConfigError, match="Provider name does not match"):
        create_default_provider(valid_config(), registry)


def test_create_named_provider():
    from image_ai.config_loader import create_provider

    config = valid_config()
    config["providers"]["second"] = {
        "enabled": True,
        "adapter": "test.SecondProvider",
        "model": "second-model",
    }

    class SecondProvider(DummyProvider):
        def capabilities(self):
            return ProviderCapabilities(
                provider_name="second",
                supported_mime_types=("image/png",),
                max_image_bytes=2048,
                supports_structured_output=True,
            )

    registry = make_registry()
    registry.register("second", "test.SecondProvider", SecondProvider)

    provider = create_provider(config, registry, "second")

    assert isinstance(provider, SecondProvider)


def test_create_named_disabled_provider_rejected():
    from image_ai.config_loader import create_provider

    config = valid_config()
    config["providers"]["second"] = {
        "enabled": False,
        "adapter": "test.SecondProvider",
        "model": "second-model",
    }

    registry = make_registry()

    with pytest.raises(AIConfigError, match="Provider is disabled"):
        create_provider(config, registry, "second")


def test_create_named_unregistered_provider_rejected():
    from image_ai.config_loader import create_provider

    config = valid_config()
    config["providers"]["second"] = {
        "enabled": True,
        "adapter": "test.SecondProvider",
        "model": "second-model",
    }

    with pytest.raises(AIConfigError, match="Provider is not registered"):
        create_provider(config, make_registry(), "second")

import json
from pathlib import Path
from typing import Any

from .provider_registry import ProviderRegistry


class AIConfigError(Exception):
    pass


def load_config(path: str | Path) -> dict[str, Any]:
    config_path = Path(path)

    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AIConfigError(
            f"Cannot read AI config: {config_path}"
        ) from exc

    if not isinstance(config, dict):
        raise AIConfigError("AI config root must be an object")

    if config.get("config_version") != "1.0":
        raise AIConfigError("Unsupported AI config version")

    providers = config.get("providers")
    if not isinstance(providers, dict) or not providers:
        raise AIConfigError("No providers configured")

    for name, provider_config in providers.items():
        if not isinstance(name, str) or not name.strip():
            raise AIConfigError("Invalid provider name")

        if not isinstance(provider_config, dict):
            raise AIConfigError(f"Invalid provider configuration: {name}")

        if not isinstance(provider_config.get("enabled"), bool):
            raise AIConfigError(f"Provider enabled flag must be boolean: {name}")

        if not isinstance(provider_config.get("adapter"), str) or not provider_config["adapter"].strip():
            raise AIConfigError(f"Provider adapter is not configured: {name}")

        if not isinstance(provider_config.get("model"), str) or not provider_config["model"].strip():
            raise AIConfigError(f"Provider model is not configured: {name}")

    default_name = config.get("default_provider")
    if not isinstance(default_name, str) or not default_name.strip():
        raise AIConfigError("Default provider is not configured")

    default_config = providers.get(default_name)
    if not isinstance(default_config, dict):
        raise AIConfigError("Default provider does not exist")

    if default_config.get("enabled") is not True:
        raise AIConfigError("Default provider is disabled")

    return config


def create_provider(
    config: dict[str, Any],
    registry: ProviderRegistry,
    name: str,
):
    if not isinstance(name, str) or not name.strip():
        raise AIConfigError("Provider name is required")

    providers = config.get("providers")
    if not isinstance(providers, dict):
        raise AIConfigError("Provider configuration is invalid")

    provider_config = providers.get(name)
    if not isinstance(provider_config, dict):
        raise AIConfigError(f"Provider does not exist: {name}")

    if provider_config.get("enabled") is not True:
        raise AIConfigError(f"Provider is disabled: {name}")

    adapter = provider_config.get("adapter")
    model = provider_config.get("model")

    if not isinstance(adapter, str) or not adapter.strip():
        raise AIConfigError(f"Provider adapter is not configured: {name}")

    if not isinstance(model, str) or not model.strip():
        raise AIConfigError(f"Provider model is not configured: {name}")

    try:
        registered_adapter = registry.adapter_id(name)
    except Exception as exc:
        raise AIConfigError(
            f"Provider is not registered: {name}"
        ) from exc

    if registered_adapter != adapter:
        raise AIConfigError(
            f"Configured adapter does not match registry for provider: {name}"
        )

    provider = registry.create(name)

    if provider.capabilities().provider_name != name:
        raise AIConfigError(
            "Provider name does not match its registered adapter"
        )

    return provider


def create_default_provider(
    config: dict[str, Any],
    registry: ProviderRegistry,
):
    return create_provider(
        config,
        registry,
        config.get("default_provider"),
    )

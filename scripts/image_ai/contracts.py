import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator


class ContractError(Exception):
    pass


def load_schema(path: str | Path) -> dict[str, Any]:
    schema_path = Path(path)

    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(
            f"Cannot load JSON Schema: {schema_path}"
        ) from exc

    Draft202012Validator.check_schema(schema)
    return schema


def validate_payload(
    payload: dict[str, Any],
    schema: dict[str, Any],
) -> None:
    validator = Draft202012Validator(schema)
    errors = sorted(
        validator.iter_errors(payload),
        key=lambda error: list(error.absolute_path),
    )

    if errors:
        details = []
        for error in errors[:20]:
            location = ".".join(
                str(part) for part in error.absolute_path
            ) or "<root>"
            details.append(f"{location}: {error.message}")

        raise ContractError(
            "JSON contract validation failed: "
            + "; ".join(details)
        )


def validate_input(
    payload: dict[str, Any],
    schema_path: str | Path,
) -> None:
    validate_payload(payload, load_schema(schema_path))


def validate_output(
    payload: dict[str, Any],
    schema_path: str | Path,
) -> None:
    validate_payload(payload, load_schema(schema_path))

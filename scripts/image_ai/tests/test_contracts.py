import copy
import json

import pytest

from image_ai.contracts import (
    ContractError,
    load_schema,
    validate_input,
    validate_output,
)


INPUT_SCHEMA = "/opt/uniplex/config/image_analysis_input_contract_v1.json"
OUTPUT_SCHEMA = "/opt/uniplex/config/image_analysis_contract_v1.json"


def valid_input():
    return {
        "contract_version": "1.0",
        "request_id": "test-request-001",
        "task": "describe_and_extract",
        "source": {
            "attachment_id": "test-attachment",
            "sha256": "a" * 64,
            "mime_type": "image/png",
            "size_bytes": 1024,
            "filename": "test.png",
            "dimensions": {"width": 800, "height": 600},
        },
        "image_payload": {
            "transport": "binary_part",
            "part_name": "image",
        },
        "instructions": {
            "language_mode": "auto",
            "extraction_scope": "Extract visible document facts",
            "domain_context": "UNIPLEX underlay manufacturing",
        },
        "output_contract": {
            "name": "UNIPLEX Image Analysis Contract",
            "version": "1.0",
        },
        "generation": {
            "temperature": 0,
            "max_output_tokens": 2048,
        },
    }


def valid_output():
    return {
        "contract_version": "1.0",
        "image_summary": "Test image of a document",
        "detected_language": "ru",
        "document_classification": {
            "category": "invoice",
            "confidence": 95,
            "alternatives": [],
        },
        "facts": [
            {
                "item_type": "amount",
                "field_key": "total_amount",
                "value": "1250.00",
                "normalized_value": 1250.00,
                "confidence": 98,
                "source_location": {
                    "page": 1,
                    "source_text": "Итого: 1250.00",
                },
                "review_recommendation": "pending",
                "metadata": {},
            }
        ],
        "overall_review": {
            "recommendation": "pending",
            "reasons": [],
        },
        "warnings": [],
    }


def test_load_both_schemas():
    assert load_schema(INPUT_SCHEMA)["title"]
    assert load_schema(OUTPUT_SCHEMA)["title"]


def test_valid_input_is_accepted():
    validate_input(valid_input(), INPUT_SCHEMA)


def test_valid_output_is_accepted():
    validate_output(valid_output(), OUTPUT_SCHEMA)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda p: p.update(contract_version="2.0"),
        lambda p: p.update(task="other_task"),
        lambda p: p["source"].update(sha256="invalid"),
        lambda p: p["source"].update(size_bytes=0),
        lambda p: p["source"].update(mime_type="application/pdf"),
        lambda p: p["image_payload"].update(transport="base64"),
        lambda p: p["instructions"].update(language_mode="de"),
        lambda p: p["generation"].update(temperature=1.5),
        lambda p: p.update(unexpected_field=True),
    ],
)
def test_invalid_input_is_rejected(mutation):
    payload = valid_input()
    mutation(payload)

    with pytest.raises(ContractError, match="validation failed"):
        validate_input(payload, INPUT_SCHEMA)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda p: p.pop("image_summary"),
        lambda p: p["document_classification"].update(confidence=101),
        lambda p: p["facts"][0].update(confidence=-1),
        lambda p: p["facts"][0].update(field_key="Total Amount"),
        lambda p: p["facts"][0].update(item_type="unknown"),
        lambda p: p["facts"][0].update(
            source_location={"page": 1}
        ),
        lambda p: p["facts"][0].update(
            review_recommendation="accepted"
        ),
        lambda p: p.update(unexpected_field=True),
    ],
)
def test_invalid_output_is_rejected(mutation):
    payload = valid_output()
    mutation(payload)

    with pytest.raises(ContractError, match="validation failed"):
        validate_output(payload, OUTPUT_SCHEMA)


def test_output_fact_with_coordinates_is_accepted():
    payload = valid_output()
    payload["facts"][0]["source_location"] = {
        "coordinate_system": "normalized_0_1",
        "x": 0.1,
        "y": 0.2,
        "width": 0.3,
        "height": 0.1,
    }

    validate_output(payload, OUTPUT_SCHEMA)


def test_invalid_schema_is_rejected(tmp_path):
    path = tmp_path / "invalid_schema.json"
    path.write_text(
        json.dumps({"type": "not-a-valid-json-schema-type"}),
        encoding="utf-8",
    )

    with pytest.raises(Exception):
        load_schema(path)


def test_missing_schema_file_is_rejected(tmp_path):
    with pytest.raises(ContractError, match="Cannot load JSON Schema"):
        load_schema(tmp_path / "missing.json")

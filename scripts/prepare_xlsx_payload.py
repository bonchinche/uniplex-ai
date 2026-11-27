#!/usr/bin/env python3

import argparse
import json
from pathlib import Path


VERSION = "1.0"


def load_json(path):
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def cell_value_text(value):
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if value is None:
        return ""
    return str(value)


def prepare_payload(source):
    if source.get("format") != "xlsx":
        raise ValueError("Input is not an XLSX extraction result")

    sheets = source.get("sheets")
    if not isinstance(sheets, list) or not sheets:
        raise ValueError("XLSX result contains no sheets")

    extracted_text = source.get("extracted_text")
    if not isinstance(extracted_text, str) or not extracted_text.strip():
        raise ValueError("XLSX extracted_text is empty")

    segments = []
    formula_count = 0
    hyperlink_count = 0
    merged_range_count = 0
    numeric_count = 0
    string_count = 0
    date_count = 0

    for sheet in sheets:
        sheet_name = sheet.get("name")
        if not isinstance(sheet_name, str) or not sheet_name:
            raise ValueError("Sheet has no valid name")

        cells = sheet.get("cells", [])
        if not isinstance(cells, list):
            raise ValueError(f"Invalid cells list in sheet {sheet_name}")

        merged_range_count += len(sheet.get("merged_ranges", []))
        hyperlink_count += len(sheet.get("hyperlinks", []))

        for cell in cells:
            coordinate = cell.get("coordinate")
            if not isinstance(coordinate, str) or not coordinate:
                raise ValueError("Cell has no valid coordinate")

            value = cell.get("value")
            value_text = cell_value_text(value)
            formula = cell.get("formula")

            if formula:
                formula_count += 1

            data_type = cell.get("data_type")

            if data_type == "n":
                numeric_count += 1
            elif data_type == "s":
                string_count += 1
            elif data_type == "d":
                date_count += 1

            if not value_text and not formula:
                continue

            text = f"{sheet_name}!{coordinate}: {value_text}"

            if formula:
                text += f" [FORMULA: {formula}]"

            segments.append({
                "segment_type": "cell",
                "text": text,
                "source_location": {
                    "sheet": sheet_name,
                    "coordinate": coordinate,
                    "row": cell.get("row"),
                    "column": cell.get("column"),
                },
                "confidence": None,
                "review_status": "pending",
                "metadata": {
                    "value": value,
                    "data_type": cell.get("data_type"),
                    "formula": formula,
                    "number_format": cell.get("number_format"),
                    "hyperlink": cell.get("hyperlink"),
                },
            })

    if not segments:
        raise ValueError("No non-empty cells found")

    payload = {
        "processor_name": "uniplex-xlsx-extractor",
        "processor_version": "1.2",
        "status": "processed",
        "extraction_method": "openpyxl",
        "extracted_text": extracted_text,
        "extracted_structure": source,
        "quality_metrics": {
            "sheet_count": len(sheets),
            "cell_count": len(segments),
            "formula_count": formula_count,
            "hyperlink_count": hyperlink_count,
            "merged_range_count": merged_range_count,
            "numeric_cell_count": numeric_count,
            "string_cell_count": string_count,
            "other_typed_cell_count": date_count,
        },
        "settings": {
            "adapter_version": VERSION,
            "source_library": source.get("library"),
            "source_library_version": source.get("library_version"),
        },
        "segments": segments,
    }

    return payload


def main():
    parser = argparse.ArgumentParser(
        description="Prepare XLSX extraction payload for UNIPLEX writer"
    )
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    source = load_json(Path(args.input))
    payload = prepare_payload(source)

    output = Path(args.output)
    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print("XLSX PAYLOAD: CREATED")
    print("Filename:", source.get("filename"))
    print("Sheets:", payload["quality_metrics"]["sheet_count"])
    print("Segments:", len(payload["segments"]))
    print("Extracted text characters:", len(payload["extracted_text"]))
    print("Output bytes:", output.stat().st_size)


if __name__ == "__main__":
    main()

import argparse
import json
import sys
import zipfile
from datetime import date, datetime, time
from pathlib import Path

from openpyxl import load_workbook


MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 2000
MAX_CELLS_PER_SHEET = 100000


def json_value(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value

    if isinstance(value, (datetime, date, time)):
        return value.isoformat()

    return str(value)


def build_extracted_text(result):
    lines = [
        f"Документ: {result['filename']}",
        "Формат: XLSX",
    ]

    for sheet in result["sheets"]:
        lines.append("")
        lines.append(f"Лист: {sheet['name']}")

        for cell in sheet["cells"]:
            parts = []

            if cell["value"] is not None:
                parts.append(str(cell["value"]))

            if cell["formula"]:
                parts.append(f"формула: {cell['formula']}")

            if cell["hyperlink"]:
                parts.append(f"ссылка: {cell['hyperlink']}")

            if parts:
                lines.append(
                    f"{sheet['name']}!{cell['coordinate']}: "
                    + " | ".join(parts)
                )

    return "\\n".join(lines)


def validate_xlsx(path):
    if not path.is_file():
        raise ValueError("Input file does not exist")

    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("XLSX file exceeds 25 MiB limit")

    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()

        if len(entries) > MAX_ARCHIVE_ENTRIES:
            raise ValueError("Too many ZIP archive entries")

        total_size = sum(item.file_size for item in entries)

        if total_size > MAX_UNCOMPRESSED_BYTES:
            raise ValueError("Uncompressed archive exceeds 100 MiB")

        names = set(archive.namelist())

        if "[Content_Types].xml" not in names:
            raise ValueError("Not a valid XLSX package")

        if "xl/workbook.xml" not in names:
            raise ValueError("Workbook XML is missing")

        bad_file = archive.testzip()

        if bad_file:
            raise ValueError(f"Corrupted ZIP entry: {bad_file}")


def extract_xlsx(path):
    validate_xlsx(path)

    workbook_formulas = load_workbook(
        path,
        read_only=False,
        data_only=False,
    )

    workbook_values = load_workbook(
        path,
        read_only=False,
        data_only=True,
    )

    result = {
        "filename": path.name,
        "format": "xlsx",
        "library": "openpyxl",
        "library_version": "3.1.5",
        "sheets": [],
    }

    try:
        if workbook_formulas.sheetnames != workbook_values.sheetnames:
            raise ValueError("Workbook sheet structures do not match")

        for formula_sheet, value_sheet in zip(
            workbook_formulas.worksheets,
            workbook_values.worksheets,
        ):
            max_row = formula_sheet.max_row or 0
            max_column = formula_sheet.max_column or 0

            if max_row * max_column > MAX_CELLS_PER_SHEET:
                raise ValueError(
                    f"Sheet {formula_sheet.title!r} exceeds cell limit"
                )

            sheet_data = {
                "name": formula_sheet.title,
                "max_row": max_row,
                "max_column": max_column,
                "cells": [],
            }

            for formula_row, value_row in zip(
                formula_sheet.iter_rows(),
                value_sheet.iter_rows(),
            ):
                for formula_cell, value_cell in zip(
                    formula_row,
                    value_row,
                ):
                    formula = formula_cell.value
                    value = value_cell.value

                    if formula is None and value is None:
                        continue

                    is_formula = (
                        isinstance(formula, str)
                        and formula.startswith("=")
                    )

                    sheet_data["cells"].append({
                        "coordinate": formula_cell.coordinate,
                        "row": formula_cell.row,
                        "column": formula_cell.column,
                        "value": json_value(value if is_formula else formula),
                        "formula": formula if is_formula else None,
                        "data_type": formula_cell.data_type,
                        "number_format": formula_cell.number_format,
                        "hyperlink": (
                            formula_cell.hyperlink.target
                            if formula_cell.hyperlink else None
                        ),
                    })

            sheet_data["merged_ranges"] = [
                str(rng) for rng in formula_sheet.merged_cells.ranges
            ]

            sheet_data["hyperlinks"] = [
                {
                    "cell": cell.coordinate,
                    "target": cell.hyperlink.target,
                    "tooltip": cell.hyperlink.tooltip,
                }
                for row in formula_sheet.iter_rows()
                for cell in row
                if cell.hyperlink
            ]

            sheet_data["hidden_rows"] = [
                index for index, dimension
                in formula_sheet.row_dimensions.items()
                if dimension.hidden
            ]

            sheet_data["hidden_columns"] = [
                key for key, dimension
                in formula_sheet.column_dimensions.items()
                if dimension.hidden
            ]

            sheet_data["freeze_panes"] = (
                str(formula_sheet.freeze_panes)
                if formula_sheet.freeze_panes else None
            )

            sheet_data["auto_filter"] = formula_sheet.auto_filter.ref

            sheet_data["tables"] = list(formula_sheet.tables.keys())

            result["sheets"].append(sheet_data)

    finally:
        workbook_formulas.close()
        workbook_values.close()

    result['extracted_text'] = build_extracted_text(result)
    return result


def main():
    parser = argparse.ArgumentParser(
        description="Extract text and cell data from XLSX"
    )
    parser.add_argument("--input", required=True)
    args = parser.parse_args()

    try:
        result = extract_xlsx(Path(args.input))
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as error:
        print(
            f"XLSX EXTRACTION ERROR: {error}",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3

import argparse
import csv
import difflib
import io
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import pypdf
import pypdfium2 as pdfium


VERSION = "1.0"
MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_PAGES = 100
MAX_WORDS_PER_PAGE = 50000


def normalize_text(value):
    return re.sub(r"[\W_]+", "", value.lower(), flags=re.UNICODE)


def compare_text(native_text, ocr_text):
    a = normalize_text(native_text)
    b = normalize_text(ocr_text)

    if not a or not b:
        return 0.0

    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


def run_ocr(image_path, language, psm, width, height):
    result = subprocess.run(
        [
            "tesseract",
            str(image_path),
            "stdout",
            "-l", language,
            "--psm", str(psm),
            "tsv",
    ],
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )

    rows = csv.DictReader(
        io.StringIO(result.stdout),
        delimiter="\t",
    )

    words = []
    lines = {}
    confidences = []

    for row in rows:
        if row["level"] != "5":
            continue

        text = row["text"].strip()
        if not text:
            continue

        confidence = float(row["conf"])

        x = int(row["left"])
        y = int(row["top"])
        w = int(row["width"])
        h = int(row["height"])

        word = {
            "text": text,
            "confidence": round(confidence, 1),
            "bbox_pixels": [x, y, w, h],
            "bbox_normalized": [
                round(x / width, 6),
                round(y / height, 6),
                round(w / width, 6),
                round(h / height, 6),
            ],
            "block": int(row["block_num"]),
            "paragraph": int(row["par_num"]),
            "line": int(row["line_num"]),
        }

        words.append(word)

        key = (
            word["block"],
            word["paragraph"],
            word["line"],
        )
        lines.setdefault(key, []).append(text)

        if confidence >= 0:
            confidences.append(confidence)

        if len(words) >= MAX_WORDS_PER_PAGE:
            break

    line_text = [
        " ".join(parts)
        for _, parts in sorted(lines.items())
    ]

    average = (
        sum(confidences) / len(confidences)
        if confidences else None
    )

    return {
        "text": "\n".join(line_text),
        "words": words,
        "quality": {
            "word_count": len(words),
            "confidence_count": len(confidences),
            "average_confidence": (
                round(average, 1) if average is not None else None
            ),
            "low_confidence_words": sum(
                value < 60 for value in confidences
            ),
            "high_confidence_words": sum(
                value >= 85 for value in confidences
            ),
            "low_confidence_ratio": (
                round(
                    sum(value < 60 for value in confidences)
                    / len(confidences),
                    4,
                )
                if confidences else None
            ),
            "words_truncated": len(words) >= MAX_WORDS_PER_PAGE,
        },
    }


def extract_pdf(filename, language, psm, dpi):
    path = Path(filename)

    if not path.is_file():
        raise ValueError("Input file does not exist")

    file_size = path.stat().st_size

    if file_size <= 0 or file_size > MAX_FILE_BYTES:
        raise ValueError("PDF size is empty or exceeds 25 MiB")

    reader = pypdf.PdfReader(str(path), strict=False)

    if reader.is_encrypted:
        raise ValueError("Encrypted PDF requires manual review")

    page_count = len(reader.pages)

    if page_count == 0:
        raise ValueError("PDF contains no pages")

    if page_count > MAX_PAGES:
        raise ValueError("PDF exceeds the 100-page limit")

    document = pdfium.PdfDocument(str(path))

    if len(document) != page_count:
        document.close()
        raise ValueError("PDF page count mismatch between libraries")

    output_pages = []
    warnings = []

    try:
        for index in range(page_count):
            page_number = index + 1
            native_text = ""

            try:
                native_text = reader.pages[index].extract_text() or ""
            except Exception as exc:
                warnings.append(
                    f"Page {page_number}: native text extraction failed: "
                    f"{type(exc).__name__}"
                )

            page = document[index]
            bitmap = None

            try:
                bitmap = page.render(scale=dpi / 72)
                image = bitmap.to_pil().convert("RGB")
                width, height = image.size

                with tempfile.TemporaryDirectory(
                    prefix="uniplex-pdf-"
                ) as temp_dir:
                    image_path = Path(temp_dir) / "page.png"
                    image.save(image_path, format="PNG")

                    ocr = run_ocr(
                        image_path,
                        language,
                        psm,
                        width,
                        height,
                    )
            except subprocess.TimeoutExpired:
                raise ValueError(
                    f"OCR timeout on page {page_number}"
                )
            except subprocess.CalledProcessError as exc:
                error = exc.stderr[-1000:] if exc.stderr else ""
                raise ValueError(
                    f"OCR failed on page {page_number}: {error}"
                )
            finally:
                if bitmap is not None:
                    bitmap.close()
                page.close()

            similarity = compare_text(native_text, ocr["text"])
            ocr_quality = ocr["quality"]
            average = ocr_quality["average_confidence"]
            low_ratio = ocr_quality["low_confidence_ratio"]

            if not native_text.strip():
                selected_text = ocr["text"]
                selected_method = "tesseract"
                reason = "native_text_absent"
            elif similarity < 0.70:
                selected_text = ocr["text"]
                selected_method = "tesseract"
                reason = "native_ocr_text_mismatch"
            else:
                selected_text = native_text
                selected_method = "pypdf"
                reason = "native_text_selected"

            page_status = "processed"

            if not selected_text.strip():
                page_status = "needs_review"
            elif selected_method == "tesseract" and (
                average is None
                or average < 80
                or low_ratio is None
                or low_ratio > 0.15
            ):
                page_status = "needs_review"

            output_pages.append({
                "page_number": page_number,
                "dimensions_pixels": [width, height],
                "native_text": native_text,
                "native_text_characters": len(native_text),
                "ocr_text": ocr["text"],
                "ocr_quality": ocr_quality,
                "ocr_words": ocr["words"],
                "native_ocr_similarity": round(similarity, 4),
                "selected_text": selected_text,
                "selected_method": selected_method,
                "selection_reason": reason,
                "status": page_status,
            })

    finally:
        document.close()

    extracted_text = "\n\n".join(
        page["selected_text"]
        for page in output_pages
        if page["selected_text"].strip()
    )

    if any(page["status"] == "needs_review" for page in output_pages):
        overall_status = "needs_review"
    else:
        overall_status = "processed"

    return {
        "filename": path.name,
        "format": "pdf",
        "processor": "uniplex-pdf-extractor",
        "processor_version": VERSION,
        "libraries": {
            "pypdf": pypdf.__version__,
            "pypdfium2": pdfium.V_PYPDFIUM2,
        },
        "file_size_bytes": file_size,
        "page_count": page_count,
        "ocr": {
            "engine": "tesseract",
            "language": language,
            "page_segmentation_mode": psm,
            "dpi": dpi,
        },
        "status": overall_status,
        "warnings": warnings,
        "extracted_text": extracted_text,
        "pages": output_pages,
    }


def main():
    parser = argparse.ArgumentParser(
        description="UNIPLEX PDF Extractor"
    )
    parser.add_argument("input", help="Path to PDF file")
    parser.add_argument("--language", default="rus+eng")
    parser.add_argument("--psm", type=int, default=3)
    parser.add_argument("--dpi", type=int, default=300)

    args = parser.parse_args()

    if args.dpi < 150 or args.dpi > 400:
        parser.error("--dpi must be between 150 and 400")

    if args.psm not in (3, 4, 6, 11):
        parser.error("--psm must be 3, 4, 6 or 11")

    try:
        result = extract_pdf(
            args.input,
            args.language,
            args.psm,
            args.dpi,
        )
        json.dump(
            result,
            sys.stdout,
            ensure_ascii=False,
            indent=2,
        )
        sys.stdout.write("\n")
    except Exception as exc:
        print(
            f"PDF_EXTRACTION_ERROR: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        sys.exit(2)


if __name__ == "__main__":
    main()

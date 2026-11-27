import argparse
import json
from pathlib import Path


def prepare_payload(result):
    if result.get("format") != "pdf":
        raise ValueError("Input is not a PDF extraction result")

    pages = result.get("pages")

    if not isinstance(pages, list) or len(pages) != result.get("page_count"):
        raise ValueError("PDF page count mismatch")

    methods = sorted({
        page["selected_method"]
        for page in pages
    })

    extraction_method = (
        methods[0] if len(methods) == 1 else "mixed"
    )

    total_words = 0
    confidence_count = 0
    confidence_sum = 0.0
    low_confidence_words = 0
    high_confidence_words = 0
    pages_needs_review = 0

    segments = []

    for page in pages:
        quality = page.get("ocr_quality") or {}

        total_words += quality.get("word_count", 0)
        confidence_count += quality.get("confidence_count", 0)

        average = quality.get("average_confidence")
        if average is not None:
            confidence_sum += average * quality.get("confidence_count", 0)

        low_confidence_words += quality.get("low_confidence_words", 0)
        high_confidence_words += quality.get("high_confidence_words", 0)

        if page.get("status") == "needs_review":
            pages_needs_review += 1

        selected_method = page["selected_method"]

        confidence = (
            average
            if selected_method == "tesseract"
            else None
        )

        segments.append({
            "segment_type": "page",
            "text": page.get("selected_text", ""),
            "source_location": {
                "page": page["page_number"],
            },
            "confidence": confidence,
            "review_status": (
                "needs_review"
                if page.get("status") == "needs_review"
                else "pending"
            ),
            "metadata": {
                "dimensions_pixels": page.get("dimensions_pixels"),
                "selected_method": selected_method,
                "selection_reason": page.get("selection_reason"),
                "native_text_characters": page.get(
                    "native_text_characters"
                ),
                "native_ocr_similarity": page.get(
                    "native_ocr_similarity"
                ),
                "ocr_quality": quality,
                "ocr_word_count": len(page.get("ocr_words", [])),
            },
        })

    weighted_average = (
        round(confidence_sum / confidence_count, 2)
        if confidence_count else None
    )

    quality_metrics = {
        "page_count": len(pages),
        "pages_needs_review": pages_needs_review,
        "pages_processed": len(pages) - pages_needs_review,
        "ocr_word_count": total_words,
        "ocr_confidence_count": confidence_count,
        "ocr_average_confidence": weighted_average,
        "ocr_low_confidence_words": low_confidence_words,
        "ocr_high_confidence_words": high_confidence_words,
        "ocr_low_confidence_ratio": (
            round(low_confidence_words / confidence_count, 4)
            if confidence_count else None
        ),
        "methods": {
            method: sum(
                page["selected_method"] == method
                for page in pages
            )
            for method in methods
        },
    }

    payload = {
        "processor_name": result["processor"],
        "processor_version": result["processor_version"],
        "status": result["status"],
        "extraction_method": extraction_method,
        "extracted_text": result["extracted_text"],
        "extracted_structure": result,
        "quality_metrics": quality_metrics,
        "settings": {
            "ocr": result.get("ocr", {}),
            "libraries": result.get("libraries", {}),
        },
        "segments": segments,
    }

    return payload


def main():
    parser = argparse.ArgumentParser(
        description="Convert PDF extractor JSON to UNIPLEX document contract"
    )
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    with Path(args.input).open("r", encoding="utf-8") as file:
        result = json.load(file)

    payload = prepare_payload(result)

    destination = Path(args.output)
    destination.parent.mkdir(parents=True, exist_ok=True)

    with destination.open("w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)

    metrics = payload["quality_metrics"]
    print("PAYLOAD: CREATED")
    print("Filename:", result["filename"])
    print("Status:", payload["status"])
    print("Extraction method:", payload["extraction_method"])
    print("Pages:", len(payload["segments"]))
    print("OCR words:", metrics["ocr_word_count"])
    print("Pages needing review:", metrics["pages_needs_review"])
    print("Extracted text characters:", len(payload["extracted_text"]))
    print("Output bytes:", destination.stat().st_size)


if __name__ == "__main__":
    main()

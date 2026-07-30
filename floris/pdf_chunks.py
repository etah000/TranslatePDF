from __future__ import annotations

from pathlib import Path

import pymupdf


def count_pages(pdf_path: Path) -> int:
    doc = pymupdf.open(pdf_path)
    try:
        return doc.page_count
    finally:
        doc.close()


def split_pdf_chunk(
    source_pdf_path: Path,
    output_pdf_path: Path,
    start_page: int,
    end_page: int,
) -> None:
    if start_page < 1:
        raise ValueError("start_page must be >= 1")
    if end_page < start_page:
        raise ValueError("end_page must be >= start_page")

    output_pdf_path.parent.mkdir(parents=True, exist_ok=True)
    source = pymupdf.open(source_pdf_path)
    chunk = pymupdf.open()
    try:
        if end_page > source.page_count:
            raise ValueError("end_page exceeds source page count")
        chunk.insert_pdf(source, from_page=start_page - 1, to_page=end_page - 1)
        chunk.save(output_pdf_path)
    finally:
        chunk.close()
        source.close()


def merge_pdfs(input_paths: list[Path], output_path: Path) -> None:
    if not input_paths:
        raise ValueError("input_paths must not be empty")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    merged = pymupdf.open()
    try:
        for input_path in input_paths:
            doc = pymupdf.open(input_path)
            try:
                merged.insert_pdf(doc)
            finally:
                doc.close()
        merged.save(output_path)
    finally:
        merged.close()

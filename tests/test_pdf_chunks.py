from pathlib import Path

import pymupdf

from floris.pdf_chunks import count_pages
from floris.pdf_chunks import merge_pdfs
from floris.pdf_chunks import split_pdf_chunk


def _make_pdf(path: Path, pages: int) -> None:
    doc = pymupdf.open()
    try:
        for i in range(pages):
            page = doc.new_page()
            page.insert_text((72, 72), f"page {i + 1}")
        doc.save(path)
    finally:
        doc.close()


def test_split_pdf_chunk_uses_one_based_pages(tmp_path: Path):
    source = tmp_path / "source.pdf"
    chunk = tmp_path / "chunk.pdf"
    _make_pdf(source, 5)

    split_pdf_chunk(source, chunk, 2, 4)

    assert count_pages(chunk) == 3


def test_merge_pdfs_keeps_order(tmp_path: Path):
    first = tmp_path / "first.pdf"
    second = tmp_path / "second.pdf"
    merged = tmp_path / "merged.pdf"
    _make_pdf(first, 2)
    _make_pdf(second, 1)

    merge_pdfs([first, second], merged)

    assert count_pages(merged) == 3

from __future__ import annotations

import csv
import io
import logging
import re
from collections.abc import Callable
from pathlib import Path

import pymupdf
from babeldoc.glossary import Glossary

log = logging.getLogger(__name__)

CSV_HEADER = "source,target,tgt_lng\n"
GLOSSARY_HEADINGS = ("glossary", "index of terms", "术语表", "词汇表")


def _extract_tail_text(source_pdf_path: Path, max_pages: int = 80) -> str:
    try:
        doc = pymupdf.open(source_pdf_path)
    except Exception:
        return ""
    try:
        start = max(0, doc.page_count - max_pages)
        return "\n".join(doc[i].get_text("text") for i in range(start, doc.page_count))
    except Exception:
        log.debug("failed to extract tail text from %s", source_pdf_path, exc_info=True)
        return ""
    finally:
        doc.close()


def _csv_has_entries(csv_path: Path) -> bool:
    try:
        with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            return any((row.get("source") or "").strip() for row in reader)
    except Exception:
        return False


def _extract_glossary_csv_from_tail(text: str, lang_out: str) -> str | None:
    lower = text.lower()
    heading_index = min(
        (lower.find(heading) for heading in GLOSSARY_HEADINGS if lower.find(heading) >= 0),
        default=-1,
    )
    if heading_index < 0:
        return None

    entries: list[tuple[str, str, str]] = []
    tail = text[heading_index:]
    for line in tail.splitlines():
        line = line.strip()
        if not line or len(line) > 160:
            continue
        match = re.match(r"^(.{2,80}?)(?:\s+-\s+|\s+--\s+|\s+:\s+)(.{1,80})$", line)
        if match:
            entries.append((match.group(1).strip(), match.group(2).strip(), lang_out))
        if len(entries) >= 500:
            break

    if not entries:
        return None

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["source", "target", "tgt_lng"])
    writer.writerows(entries)
    return buffer.getvalue()


def ensure_book_glossary(
    source_pdf_path: Path,
    job_dir: Path,
    lang_out: str,
    generator: Callable[[str, str], str] | None = None,
) -> Path:
    job_dir.mkdir(parents=True, exist_ok=True)
    glossary_path = job_dir / "glossary.csv"
    if glossary_path.exists():
        return glossary_path

    text = _extract_tail_text(source_pdf_path)
    csv_text = _extract_glossary_csv_from_tail(text, lang_out)
    if csv_text is None and generator is not None:
        try:
            csv_text = generator(text, lang_out)
        except Exception:
            log.warning("glossary generator failed for %s", source_pdf_path, exc_info=True)
            csv_text = None

    if not csv_text:
        csv_text = CSV_HEADER
    if not csv_text.startswith("source,target"):
        csv_text = CSV_HEADER

    glossary_path.write_text(csv_text, encoding="utf-8")
    return glossary_path


def load_babeldoc_glossary(csv_path: Path, lang_out: str) -> Glossary | None:
    if not csv_path.exists() or not _csv_has_entries(csv_path):
        return None
    return Glossary.from_csv(csv_path, lang_out)

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class UploadDecision:
    path: Path
    should_write: bool
    reused_existing: bool


def _safe_pdf_name(filename: str) -> str:
    raw_path = Path(filename)
    if raw_path.suffix.lower() != ".pdf":
        raise ValueError("file must be a PDF")
    safe_stem = re.sub(r"[^a-zA-Z0-9._-]", "_", raw_path.stem).strip("_")
    return f"{safe_stem or 'document'}.pdf"


def resolve_upload_path(
    upload_dir: Path,
    filename: str,
    policy: str,
) -> UploadDecision:
    if policy not in {"reuse", "overwrite", "dedupe"}:
        raise ValueError("upload policy must be one of reuse, overwrite, dedupe")

    upload_dir = Path(upload_dir)
    target = upload_dir / _safe_pdf_name(filename)
    if not target.exists():
        return UploadDecision(target, should_write=True, reused_existing=False)

    if policy == "reuse":
        return UploadDecision(target, should_write=False, reused_existing=True)
    if policy == "overwrite":
        return UploadDecision(target, should_write=True, reused_existing=False)

    stem = target.stem
    while True:
        candidate = upload_dir / f"{stem}-{uuid.uuid4().hex[:6]}.pdf"
        if not candidate.exists():
            return UploadDecision(candidate, should_write=True, reused_existing=False)

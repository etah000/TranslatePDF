from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from typing import Any


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def chunk_ranges(total_pages: int, chunk_size: int) -> list[tuple[int, int]]:
    if total_pages < 1:
        return []
    if chunk_size < 1:
        raise ValueError("chunk_size must be >= 1")
    ranges = []
    for start in range(1, total_pages + 1, chunk_size):
        end = min(start + chunk_size - 1, total_pages)
        ranges.append((start, end))
    return ranges


@dataclass
class ChunkState:
    index: int
    start_page: int
    end_page: int
    status: str = "pending"
    mono_pdf_path: str | None = None
    dual_pdf_path: str | None = None
    error: str | None = None
    updated_at: datetime | None = None

    @property
    def pages(self) -> str:
        return f"{self.start_page}-{self.end_page}"

    @property
    def chunk_name(self) -> str:
        return f"chunk_{self.index:04d}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "pages": self.pages,
            "start_page": self.start_page,
            "end_page": self.end_page,
            "status": self.status,
            "mono_pdf_path": self.mono_pdf_path,
            "dual_pdf_path": self.dual_pdf_path,
            "error": self.error,
            "updated_at": (self.updated_at or _now_utc()).isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ChunkState":
        start_page = data.get("start_page")
        end_page = data.get("end_page")
        if start_page is None or end_page is None:
            start_text, end_text = str(data["pages"]).split("-", maxsplit=1)
            start_page = int(start_text)
            end_page = int(end_text)
        updated = data.get("updated_at")
        return cls(
            index=int(data["index"]),
            start_page=int(start_page),
            end_page=int(end_page),
            status=data.get("status", "pending"),
            mono_pdf_path=data.get("mono_pdf_path"),
            dual_pdf_path=data.get("dual_pdf_path"),
            error=data.get("error"),
            updated_at=_parse_dt(updated) if updated else None,
        )


@dataclass
class BookJobManifest:
    job_id: str
    source_pdf_path: Path
    job_dir: Path
    chunk_size: int
    total_pages: int
    status: str
    created_at: datetime
    updated_at: datetime
    chunks: list[ChunkState]

    @classmethod
    def new(
        cls,
        job_id: str,
        source_pdf_path: Path,
        job_dir: Path,
        chunk_size: int,
        total_pages: int,
    ) -> "BookJobManifest":
        now = _now_utc()
        chunks = [
            ChunkState(index=i + 1, start_page=start, end_page=end, updated_at=now)
            for i, (start, end) in enumerate(chunk_ranges(total_pages, chunk_size))
        ]
        return cls(
            job_id=job_id,
            source_pdf_path=Path(source_pdf_path),
            job_dir=Path(job_dir),
            chunk_size=chunk_size,
            total_pages=total_pages,
            status="pending",
            created_at=now,
            updated_at=now,
            chunks=chunks,
        )

    @property
    def manifest_path(self) -> Path:
        return self.job_dir / "manifest.json"

    @property
    def glossary_path(self) -> Path:
        return self.job_dir / "glossary.csv"

    @property
    def final_mono_pdf_path(self) -> Path:
        return self.job_dir / "final.mono.pdf"

    @property
    def final_dual_pdf_path(self) -> Path:
        return self.job_dir / "final.dual.pdf"

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "source_pdf_path": str(self.source_pdf_path),
            "job_dir": str(self.job_dir),
            "status": self.status,
            "chunk_size": self.chunk_size,
            "total_pages": self.total_pages,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "glossary_path": str(self.glossary_path),
            "final": {
                "mono_pdf_path": (
                    str(self.final_mono_pdf_path)
                    if self.final_mono_pdf_path.exists()
                    else None
                ),
                "dual_pdf_path": (
                    str(self.final_dual_pdf_path)
                    if self.final_dual_pdf_path.exists()
                    else None
                ),
            },
            "chunks": [chunk.to_dict() for chunk in self.chunks],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BookJobManifest":
        return cls(
            job_id=data["job_id"],
            source_pdf_path=Path(data["source_pdf_path"]),
            job_dir=Path(data.get("job_dir") or Path(data["source_pdf_path"]).parent),
            chunk_size=int(data["chunk_size"]),
            total_pages=int(data["total_pages"]),
            status=data.get("status", "pending"),
            created_at=_parse_dt(data["created_at"]),
            updated_at=_parse_dt(data["updated_at"]),
            chunks=[ChunkState.from_dict(item) for item in data.get("chunks", [])],
        )


def save_manifest(manifest: BookJobManifest) -> None:
    manifest.updated_at = manifest.updated_at or _now_utc()
    manifest.job_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = manifest.manifest_path.with_suffix(".json.tmp")
    tmp_path.write_text(
        json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp_path.replace(manifest.manifest_path)


def load_manifest(path: Path) -> BookJobManifest:
    return BookJobManifest.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


def first_unfinished_chunk(manifest: BookJobManifest) -> ChunkState | None:
    for chunk in manifest.chunks:
        if chunk.status != "done":
            return chunk
    return None


def cleanup_completed_chunk_dirs(manifest: BookJobManifest) -> None:
    chunks_dir = manifest.job_dir / "chunks"
    if chunks_dir.exists():
        shutil.rmtree(chunks_dir)


def cleanup_stale_incomplete_jobs(
    jobs_dir: Path,
    now: datetime,
    ttl: timedelta,
) -> list[Path]:
    removed = []
    for manifest_path in sorted(Path(jobs_dir).glob("*/manifest.json")):
        manifest = load_manifest(manifest_path)
        if manifest.status == "done":
            continue
        if now - manifest.updated_at >= ttl:
            shutil.rmtree(manifest.job_dir)
            removed.append(manifest.job_dir)
    return removed

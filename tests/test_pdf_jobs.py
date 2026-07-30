from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path

from floris.pdf_jobs import BookJobManifest
from floris.pdf_jobs import chunk_ranges
from floris.pdf_jobs import cleanup_completed_chunk_dirs
from floris.pdf_jobs import cleanup_stale_incomplete_jobs
from floris.pdf_jobs import first_unfinished_chunk
from floris.pdf_jobs import load_manifest
from floris.pdf_jobs import save_manifest


def test_chunk_ranges_uses_one_based_inclusive_pages():
    assert chunk_ranges(total_pages=121, chunk_size=50) == [
        (1, 50),
        (51, 100),
        (101, 121),
    ]


def test_manifest_round_trip_and_resume(tmp_path: Path):
    manifest = BookJobManifest.new(
        job_id="job1",
        source_pdf_path=tmp_path / "book.pdf",
        job_dir=tmp_path / "job1",
        chunk_size=50,
        total_pages=75,
    )
    manifest.chunks[0].status = "done"
    save_manifest(manifest)

    loaded = load_manifest(manifest.manifest_path)

    assert loaded.job_id == "job1"
    assert first_unfinished_chunk(loaded).index == 2


def test_completed_cleanup_preserves_manifest_glossary_and_final(tmp_path: Path):
    manifest = BookJobManifest.new(
        job_id="job1",
        source_pdf_path=tmp_path / "book.pdf",
        job_dir=tmp_path / "job1",
        chunk_size=50,
        total_pages=10,
    )
    chunk_dir = manifest.job_dir / "chunks" / "chunk_0001"
    chunk_dir.mkdir(parents=True)
    (chunk_dir / "input.pdf").write_bytes(b"x")
    manifest.glossary_path.parent.mkdir(parents=True, exist_ok=True)
    manifest.glossary_path.write_text("source,target,tgt_lng\n", encoding="utf-8")
    manifest.final_mono_pdf_path.write_bytes(b"mono")
    save_manifest(manifest)

    cleanup_completed_chunk_dirs(manifest)

    assert not chunk_dir.exists()
    assert manifest.manifest_path.exists()
    assert manifest.glossary_path.exists()
    assert manifest.final_mono_pdf_path.exists()


def test_stale_incomplete_cleanup_removes_old_running_job(tmp_path: Path):
    jobs_dir = tmp_path / "jobs"
    old_job = BookJobManifest.new(
        job_id="old",
        source_pdf_path=tmp_path / "book.pdf",
        job_dir=jobs_dir / "old",
        chunk_size=50,
        total_pages=10,
    )
    old_job.status = "running"
    old_job.updated_at = datetime(2026, 7, 1, tzinfo=UTC)
    save_manifest(old_job)

    removed = cleanup_stale_incomplete_jobs(
        jobs_dir,
        now=datetime(2026, 7, 10, tzinfo=UTC),
        ttl=timedelta(days=7),
    )

    assert removed == [old_job.job_dir]
    assert not old_job.job_dir.exists()

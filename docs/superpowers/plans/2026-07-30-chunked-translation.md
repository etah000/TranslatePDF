# Chunked Translation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a book-level 50-page chunked translation mode with cached glossary, resumable manifest, final PDF merge, and lifecycle cleanup.

**Architecture:** Add focused helper modules around the existing `serve.py` runner instead of rewriting BabelDOC. The orchestrator physically splits PDFs into chunks, runs BabelDOC once per chunk, records durable state in `manifest.json`, merges completed chunk PDFs, and cleans chunk temporary files after success.

**Tech Stack:** Python 3.10+, FastAPI, PyMuPDF, BabelDOC, pytest, SQLite via BabelDOC's existing peewee cache.

## Global Constraints

- Translate large PDFs in 50-page physical chunks by default.
- Resume from the first unfinished chunk after interruption.
- Prefer a glossary found in the book itself. If no book glossary is found, generate one with an LLM.
- Cache the generated glossary for the book job and reuse it for every chunk.
- If an uploaded filename already exists under `uploads/`, let the user reuse or overwrite it; default to reuse.
- Increase BabelDOC's global SQLite translation cache cap to `100000` rows.
- Do not delete the global SQLite translation text cache when a book job completes.
- After a full book job completes, delete chunk temporary files while keeping final PDFs, `manifest.json`, and `glossary.csv`.
- Delete incomplete book job artifacts after 7 days without progress.
- Do not introduce RocksDB in the first implementation.

---

## File Structure

- Create `floris/pdf_jobs.py`: job manifest dataclasses, atomic JSON persistence, chunk range calculation, stale cleanup, chunk-temp cleanup.
- Create `floris/pdf_chunks.py`: PyMuPDF helpers for page counting, physical chunk splitting, and PDF merging.
- Create `floris/glossary.py`: book glossary discovery fallback, LLM glossary generation hook, CSV persistence/load helpers.
- Modify `serve.py`: import new helpers, add chunked task path, upload policy handling, chunked SSE events, download final outputs.
- Modify `/data/opensource/BabelDOC/babeldoc/translator/cache.py`: set `MAX_CACHE_ROWS = 100_000` and disable random cleanup during active translation.
- Create `tests/test_pdf_jobs.py`: unit tests for manifest, ranges, cleanup.
- Create `tests/test_pdf_chunks.py`: unit tests for split/merge with small generated PDFs.
- Create `tests/test_upload_policy.py`: unit tests for upload path policy helper.

---

### Task 1: Job Manifest And Cleanup Helpers

**Files:**
- Create: `floris/pdf_jobs.py`
- Test: `tests/test_pdf_jobs.py`

**Interfaces:**
- Produces: `ChunkState`, `BookJobManifest`, `chunk_ranges(total_pages: int, chunk_size: int) -> list[tuple[int, int]]`
- Produces: `save_manifest(manifest: BookJobManifest) -> None`
- Produces: `load_manifest(path: Path) -> BookJobManifest`
- Produces: `first_unfinished_chunk(manifest: BookJobManifest) -> ChunkState | None`
- Produces: `cleanup_completed_chunk_dirs(manifest: BookJobManifest) -> None`
- Produces: `cleanup_stale_incomplete_jobs(jobs_dir: Path, now: datetime, ttl: timedelta) -> list[Path]`

- [ ] **Step 1: Write failing tests**

```python
from datetime import UTC, datetime, timedelta
from pathlib import Path

from floris.pdf_jobs import BookJobManifest
from floris.pdf_jobs import ChunkState
from floris.pdf_jobs import chunk_ranges
from floris.pdf_jobs import cleanup_completed_chunk_dirs
from floris.pdf_jobs import cleanup_stale_incomplete_jobs
from floris.pdf_jobs import first_unfinished_chunk
from floris.pdf_jobs import load_manifest
from floris.pdf_jobs import save_manifest


def test_chunk_ranges_uses_one_based_inclusive_pages():
    assert chunk_ranges(total_pages=121, chunk_size=50) == [(1, 50), (51, 100), (101, 121)]


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
    manifest = BookJobManifest.new("job1", tmp_path / "book.pdf", tmp_path / "job1", 50, 10)
    chunk_dir = manifest.job_dir / "chunks" / "chunk_0001"
    chunk_dir.mkdir(parents=True)
    (chunk_dir / "input.pdf").write_bytes(b"x")
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
    old_job = BookJobManifest.new("old", tmp_path / "book.pdf", jobs_dir / "old", 50, 10)
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
```

- [ ] **Step 2: Run tests and verify failure**

Run: `pytest tests/test_pdf_jobs.py -v`

Expected: imports fail because `floris.pdf_jobs` does not exist.

- [ ] **Step 3: Implement `floris/pdf_jobs.py`**

Implement dataclasses with `to_dict()` and `from_dict()` methods. Write manifests atomically via `tmp.replace(target)`. Use UTC ISO timestamps. Delete only `manifest.job_dir / "chunks"` in `cleanup_completed_chunk_dirs`.

- [ ] **Step 4: Run tests and verify pass**

Run: `pytest tests/test_pdf_jobs.py -v`

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add floris/pdf_jobs.py tests/test_pdf_jobs.py
git commit -m "feat: add chunked job manifest helpers"
```

---

### Task 2: PDF Split And Merge Helpers

**Files:**
- Create: `floris/pdf_chunks.py`
- Test: `tests/test_pdf_chunks.py`

**Interfaces:**
- Consumes: page ranges as one-based inclusive tuples from `chunk_ranges`
- Produces: `count_pages(pdf_path: Path) -> int`
- Produces: `split_pdf_chunk(source_pdf_path: Path, output_pdf_path: Path, start_page: int, end_page: int) -> None`
- Produces: `merge_pdfs(input_paths: list[Path], output_path: Path) -> None`

- [ ] **Step 1: Write failing tests**

```python
from pathlib import Path

import pymupdf

from floris.pdf_chunks import count_pages
from floris.pdf_chunks import merge_pdfs
from floris.pdf_chunks import split_pdf_chunk


def _make_pdf(path: Path, pages: int) -> None:
    doc = pymupdf.open()
    for i in range(pages):
        page = doc.new_page()
        page.insert_text((72, 72), f"page {i + 1}")
    doc.save(path)
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
```

- [ ] **Step 2: Run tests and verify failure**

Run: `pytest tests/test_pdf_chunks.py -v`

Expected: imports fail because `floris.pdf_chunks` does not exist.

- [ ] **Step 3: Implement `floris/pdf_chunks.py`**

Use `pymupdf.open()`, `insert_pdf(source, from_page=start_page - 1, to_page=end_page - 1)`, and create parent directories before saving.

- [ ] **Step 4: Run tests and verify pass**

Run: `pytest tests/test_pdf_chunks.py -v`

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add floris/pdf_chunks.py tests/test_pdf_chunks.py
git commit -m "feat: add pdf chunk split and merge helpers"
```

---

### Task 3: Upload Policy Helper

**Files:**
- Create: `floris/uploads.py`
- Test: `tests/test_upload_policy.py`

**Interfaces:**
- Produces: `UploadDecision`
- Produces: `resolve_upload_path(upload_dir: Path, filename: str, policy: str) -> UploadDecision`
- `policy` accepts `reuse`, `overwrite`, and `dedupe`

- [ ] **Step 1: Write failing tests**

```python
from pathlib import Path

from floris.uploads import resolve_upload_path


def test_reuse_existing_upload(tmp_path: Path):
    existing = tmp_path / "book.pdf"
    existing.write_bytes(b"old")

    decision = resolve_upload_path(tmp_path, "book.pdf", "reuse")

    assert decision.path == existing
    assert decision.should_write is False
    assert decision.reused_existing is True


def test_overwrite_existing_upload(tmp_path: Path):
    existing = tmp_path / "book.pdf"
    existing.write_bytes(b"old")

    decision = resolve_upload_path(tmp_path, "book.pdf", "overwrite")

    assert decision.path == existing
    assert decision.should_write is True
    assert decision.reused_existing is False


def test_dedupe_existing_upload(tmp_path: Path):
    (tmp_path / "book.pdf").write_bytes(b"old")

    decision = resolve_upload_path(tmp_path, "book.pdf", "dedupe")

    assert decision.path.name.startswith("book-")
    assert decision.path.name.endswith(".pdf")
    assert decision.should_write is True
```

- [ ] **Step 2: Run tests and verify failure**

Run: `pytest tests/test_upload_policy.py -v`

Expected: imports fail because `floris.uploads` does not exist.

- [ ] **Step 3: Implement `floris/uploads.py`**

Sanitize filename stems with `re.sub(r"[^a-zA-Z0-9._-]", "_", stem).strip("_") or "document"`. Reject non-PDF filenames with `ValueError("file must be a PDF")`. For `dedupe`, append `-<six hex chars>` until the path does not exist.

- [ ] **Step 4: Run tests and verify pass**

Run: `pytest tests/test_upload_policy.py -v`

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add floris/uploads.py tests/test_upload_policy.py
git commit -m "feat: add upload reuse policy helper"
```

---

### Task 4: Glossary Cache And Generation Hook

**Files:**
- Create: `floris/glossary.py`
- Test: `tests/test_glossary_jobs.py`
- Modify: `serve.py`

**Interfaces:**
- Produces: `ensure_book_glossary(source_pdf_path: Path, job_dir: Path, lang_out: str, generator: Callable[[str, str], str] | None = None) -> Path`
- Produces: `load_babeldoc_glossary(csv_path: Path, lang_out: str) -> Glossary | None`

- [ ] **Step 1: Write failing tests**

```python
from pathlib import Path

from floris.glossary import ensure_book_glossary


def test_existing_glossary_is_reused(tmp_path: Path):
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    glossary = job_dir / "glossary.csv"
    glossary.write_text("source,target,tgt_lng\nwork,功,zh\n", encoding="utf-8")

    result = ensure_book_glossary(tmp_path / "book.pdf", job_dir, "zh")

    assert result == glossary
    assert "work" in result.read_text(encoding="utf-8")


def test_generator_creates_glossary_when_missing(tmp_path: Path):
    source = tmp_path / "book.pdf"
    source.write_bytes(b"%PDF-1.4\n")

    def generator(_text: str, lang_out: str) -> str:
        assert lang_out == "zh"
        return "source,target,tgt_lng\nmomentum,动量,zh\n"

    result = ensure_book_glossary(source, tmp_path / "job", "zh", generator=generator)

    assert result.name == "glossary.csv"
    assert "momentum" in result.read_text(encoding="utf-8")
```

- [ ] **Step 2: Run tests and verify failure**

Run: `pytest tests/test_glossary_jobs.py -v`

Expected: imports fail because `floris.glossary` does not exist.

- [ ] **Step 3: Implement `floris/glossary.py`**

First return existing `job_dir / "glossary.csv"` when present. For first version, extract plain text from the last 80 pages with PyMuPDF and search for glossary headings. If no structured glossary is found and `generator` is provided, write generator output. If generation fails or no generator exists, write only the CSV header.

- [ ] **Step 4: Wire glossary into chunk translation config**

Modify the chunked runner in `serve.py` to call `load_babeldoc_glossary(...)` and pass `glossaries=[glossary]` when the loaded glossary is non-empty.

- [ ] **Step 5: Run tests and verify pass**

Run: `pytest tests/test_glossary_jobs.py -v`

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add floris/glossary.py tests/test_glossary_jobs.py serve.py
git commit -m "feat: add cached book glossary support"
```

---

### Task 5: Chunked Translation Orchestrator

**Files:**
- Modify: `serve.py`
- Test: `tests/test_chunked_orchestrator.py`

**Interfaces:**
- Consumes: `BookJobManifest`, `split_pdf_chunk`, `merge_pdfs`, `ensure_book_glossary`
- Produces: `async def run_chunked_translation(task: Task, lang_in: str, lang_out: str, model_override: str | None = None) -> None`

- [ ] **Step 1: Write failing tests for orchestration without BabelDOC**

Use a monkeypatched async chunk translator so tests do not call LLMs:

```python
from pathlib import Path

import pytest

from floris.pdf_jobs import BookJobManifest


@pytest.mark.asyncio
async def test_chunked_runner_skips_done_chunks(tmp_path: Path, monkeypatch):
    import serve

    calls = []

    async def fake_translate_chunk(*, chunk, **_kwargs):
        calls.append(chunk.index)
        chunk.status = "done"
        chunk.mono_pdf_path = str(tmp_path / f"chunk-{chunk.index}.mono.pdf")
        Path(chunk.mono_pdf_path).write_bytes(b"pdf")

    monkeypatch.setattr(serve, "_translate_one_chunk", fake_translate_chunk)
    monkeypatch.setattr(serve, "count_pages", lambda _path: 100)
    monkeypatch.setattr(serve, "split_pdf_chunk", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(serve, "merge_pdfs", lambda inputs, output: output.write_bytes(b"merged"))

    manifest = BookJobManifest.new("job", tmp_path / "book.pdf", tmp_path / "job", 50, 100)
    manifest.chunks[0].status = "done"
    serve.save_manifest(manifest)

    await serve._run_chunked_manifest(manifest, "en", "zh", None, None)

    assert calls == [2]
    assert manifest.final_mono_pdf_path.exists()
```

- [ ] **Step 2: Run tests and verify failure**

Run: `pytest tests/test_chunked_orchestrator.py -v`

Expected: `_run_chunked_manifest` is not defined.

- [ ] **Step 3: Implement chunked orchestration in `serve.py`**

Add helpers:

```python
async def _translate_one_chunk(*, chunk: ChunkState, source_chunk_pdf: Path, output_dir: Path, lang_in: str, lang_out: str, model_override: str | None, glossary_csv: Path | None) -> None:
    ...

async def _run_chunked_manifest(manifest: BookJobManifest, lang_in: str, lang_out: str, model_override: str | None, parent_task: Task | None) -> None:
    ...
```

`_translate_one_chunk` creates a temporary `Task` for the chunk, calls the existing `run_translation`, waits for it, and copies `chunk_task.result` into the chunk state. `_run_chunked_manifest` saves the manifest after every chunk and emits parent SSE events when `parent_task` is provided.

- [ ] **Step 4: Run orchestrator tests**

Run: `pytest tests/test_chunked_orchestrator.py -v`

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add serve.py tests/test_chunked_orchestrator.py
git commit -m "feat: orchestrate chunked book translation"
```

---

### Task 6: API And Web UI Integration

**Files:**
- Modify: `serve.py`

**Interfaces:**
- Adds form fields: `chunked`, `chunk_size`, `upload_policy`
- Keeps existing `/api/tasks/{task_id}/events` and `/download/{kind}` behavior

- [ ] **Step 1: Write a focused API test**

Create `tests/test_api_chunked_upload.py` with FastAPI `TestClient`. Monkeypatch `run_chunked_translation` so the test only verifies request routing and upload policy.

- [ ] **Step 2: Run test and verify failure**

Run: `pytest tests/test_api_chunked_upload.py -v`

Expected: chunked form fields are ignored.

- [ ] **Step 3: Implement API changes**

Extend `/api/translate`:

```python
chunked: str = Form("false")
chunk_size: int = Form(50)
upload_policy: str = Form("reuse")
```

Use `resolve_upload_path(...)`. If `chunked` is true, create the parent `Task` with output dir under `outputs/jobs/<task_id>` and start `run_chunked_translation`; otherwise keep the existing single-task flow.

- [ ] **Step 4: Update Web UI**

Add controls for chunked mode, chunk size, and upload policy. Default chunked mode on for large books is not automatic in the first version; the user explicitly checks it.

- [ ] **Step 5: Run API test**

Run: `pytest tests/test_api_chunked_upload.py -v`

Expected: pass.

- [ ] **Step 6: Commit**

```bash
git add serve.py tests/test_api_chunked_upload.py
git commit -m "feat: expose chunked translation in api"
```

---

### Task 7: Cache Cap And Cleanup Policy

**Files:**
- Modify: `/data/opensource/BabelDOC/babeldoc/translator/cache.py`
- Test: `tests/test_cache_policy.py`

**Interfaces:**
- Changes: `MAX_CACHE_ROWS = 100_000`
- Changes: no random cleanup during get/set

- [ ] **Step 1: Write failing cache policy test**

```python
from pathlib import Path


def test_babeldoc_cache_row_cap_is_100000():
    text = Path("/data/opensource/BabelDOC/babeldoc/translator/cache.py").read_text(encoding="utf-8")
    assert "MAX_CACHE_ROWS = 100_000" in text
```

- [ ] **Step 2: Run test and verify failure**

Run: `pytest tests/test_cache_policy.py -v`

Expected: fails because the cap is still `50_000`.

- [ ] **Step 3: Update cache cap and disable random cleanup**

Set `MAX_CACHE_ROWS = 100_000`. Replace random cleanup calls in `get()` and `set()` with no-op comments so active large-book cache entries are not pruned mid-run.

- [ ] **Step 4: Run cache policy test**

Run: `pytest tests/test_cache_policy.py -v`

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add /data/opensource/BabelDOC/babeldoc/translator/cache.py tests/test_cache_policy.py
git commit -m "fix: raise babeldoc translation cache cap"
```

---

### Task 8: Verification

**Files:**
- No new files unless fixes are required.

**Interfaces:**
- Consumes all previous tasks.

- [ ] **Step 1: Run unit tests**

Run: `pytest tests -v`

Expected: all tests pass.

- [ ] **Step 2: Compile changed Python files**

Run: `python -m py_compile serve.py floris/pdf_jobs.py floris/pdf_chunks.py floris/uploads.py floris/glossary.py /data/opensource/BabelDOC/babeldoc/translator/cache.py`

Expected: no output and exit code 0.

- [ ] **Step 3: Run a small manual chunked translation smoke if credentials are available**

Use a 2-3 page PDF with `chunk_size=1`, verify chunk directories are created during run, final PDF is merged, and chunk dirs are removed after success.

- [ ] **Step 4: Final commit if verification fixes were needed**

```bash
git status --short
git add <changed-files>
git commit -m "test: verify chunked translation flow"
```

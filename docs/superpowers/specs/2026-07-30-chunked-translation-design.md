# Chunked PDF Translation Design

## Context

Large book-length PDFs can exceed available memory when translated as one BabelDOC task. The current service also only produces final PDFs after the whole translation, typesetting, and PDF creation pipeline completes. If the process is killed before completion, recovery depends on IL checkpoints and the global translation cache, both of which are insufficient for reliable book-level resume.

The goal is to translate large books as 50-page chunks, keep terminology consistent with a cached global glossary, support resume after interruption, and clean temporary chunk artifacts when a full book job finishes.

## Requirements

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

## Non-Goals

- No true incremental writing into a single live PDF during BabelDOC translation.
- No RocksDB cache backend.
- No job-specific deletion from BabelDOC's current global translation cache schema.
- No mandatory human approval step for generated glossary entries in the first version.

## Architecture

Add a book-level orchestration layer above BabelDOC. A book job owns upload reuse/overwrite behavior, glossary discovery/generation, chunk splitting, per-chunk execution, manifest persistence, final merge, and cleanup.

The existing BabelDOC task runner remains responsible for translating one input PDF into mono/dual output PDFs. The new orchestrator invokes it repeatedly with physically split 50-page PDFs instead of passing a page range against the full original PDF. Physical splitting is required to reduce parse/layout memory, not only translation scope.

## Job Layout

Each book job writes artifacts under:

```text
outputs/jobs/<job_id>/
  manifest.json
  glossary.csv
  final.mono.pdf
  final.dual.pdf
  chunks/
    chunk_0001/
      input.pdf
      output...
    chunk_0002/
      input.pdf
      output...
```

Temporary chunk directories may contain BabelDOC working files, chunk input PDFs, intermediate outputs, and chunk-level logs. These are deleted after the full book job finishes successfully.

## Manifest

`manifest.json` is the source of truth for resume:

```json
{
  "job_id": "opaque id",
  "source_pdf_path": "uploads/book.pdf",
  "status": "running",
  "chunk_size": 50,
  "created_at": "2026-07-30T00:00:00Z",
  "updated_at": "2026-07-30T00:00:00Z",
  "glossary_path": "outputs/jobs/<job_id>/glossary.csv",
  "final": {
    "mono_pdf_path": null,
    "dual_pdf_path": null
  },
  "chunks": [
    {
      "index": 1,
      "pages": "1-50",
      "status": "done",
      "mono_pdf_path": "outputs/jobs/<job_id>/chunks/chunk_0001/...",
      "dual_pdf_path": "outputs/jobs/<job_id>/chunks/chunk_0001/...",
      "error": null,
      "updated_at": "2026-07-30T00:00:00Z"
    }
  ]
}
```

The orchestrator flushes the manifest after each meaningful state change: glossary ready, chunk started, chunk finished, chunk failed, merge finished, cleanup finished.

## Upload Handling

When `uploads/<filename>.pdf` already exists, the API supports:

- `reuse`: skip upload and use the existing file.
- `overwrite`: replace the existing file.
- `dedupe`: keep the current behavior of adding a unique suffix.

The Web UI defaults to `reuse` and should prompt the user before overwrite.

## Glossary Flow

The job first attempts to find an existing glossary inside the book. It scans late pages and looks for headings such as `Glossary`, `Index of Terms`, `术语表`, and `词汇表`. If found, it extracts term pairs into `glossary.csv`.

If no glossary-like section is found, the service builds a candidate term list from extracted text and asks the configured LLM to produce a concise source/target glossary. The output is normalized into BabelDOC's CSV format:

```csv
source,target,tgt_lng
momentum,动量,zh_cn
work,功,zh_cn
```

Every chunk loads the same CSV with `Glossary.from_csv(...)` and passes it to `TranslationConfig(glossaries=[...])`.

## Chunk Execution

The orchestrator physically splits the source PDF into 50-page PDFs using PyMuPDF. It translates chunks sequentially by default. Sequential execution minimizes memory pressure and avoids multiple BabelDOC workers competing for memory.

If the process stops, resume reads `manifest.json`, skips completed chunks, and starts from the first non-`done` chunk. Already completed chunk PDFs are reused for final merge.

## Final Merge

After all chunks are `done`, the service merges chunk mono PDFs into `final.mono.pdf` and chunk dual PDFs into `final.dual.pdf` when those outputs exist. Merge order follows chunk index.

After successful merge, the job status becomes `done`, final paths are written to the manifest, and chunk temporary directories are deleted. The manifest and glossary remain.

## Cache Strategy

BabelDOC's global SQLite translation cache remains the translation text cache. The row cap changes from `50000` to `100000`.

The random in-flight cleanup should be disabled or made conservative enough that it does not delete active large-book entries during translation. Cleanup of job artifacts is handled separately by the book-job lifecycle.

The global SQLite cache is not cleared when a book job completes. This preserves useful translation reuse for re-runs and similar documents.

## Expiration Cleanup

On server startup, and optionally on a periodic timer, the service scans `outputs/jobs/*/manifest.json`.

If a job is not `done` and `updated_at` is older than 7 days, the service deletes that job directory. Completed jobs keep final PDFs, manifest, and glossary.

## Error Handling

If glossary generation fails, the job can continue with an empty glossary and records the glossary error in the manifest.

If a chunk fails, the job status becomes `error`, the chunk error is recorded, and completed chunks remain reusable. Resume retries the failed chunk.

If final merge fails, chunk outputs remain intact and the job can retry merge without retranslating.

## Testing

Add focused tests for:

- upload policy behavior for `reuse`, `overwrite`, and `dedupe`;
- chunk page range generation;
- manifest resume choosing the first unfinished chunk;
- final merge ordering;
- post-completion cleanup preserving final PDFs, manifest, and glossary;
- stale incomplete job cleanup after 7 days;
- cache row cap set to `100000`.

Manual validation should run a small PDF with a low chunk size, interrupt after the first chunk, resume, and verify the merged PDF contains pages in the correct order.

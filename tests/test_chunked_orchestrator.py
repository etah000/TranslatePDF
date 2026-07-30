import asyncio
from pathlib import Path

from floris.pdf_jobs import BookJobManifest
from floris.pdf_jobs import save_manifest


def test_chunked_runner_skips_done_chunks(tmp_path: Path, monkeypatch):
    import serve

    calls = []

    async def fake_translate_chunk(*, chunk, **_kwargs):
        calls.append(chunk.index)
        chunk.status = "done"
        chunk.mono_pdf_path = str(tmp_path / f"chunk-{chunk.index}.mono.pdf")
        Path(chunk.mono_pdf_path).write_bytes(b"pdf")

    def fake_merge_pdfs(inputs, output):
        assert [path.name for path in inputs] == ["chunk-1.mono.pdf", "chunk-2.mono.pdf"]
        output.write_bytes(b"merged")

    monkeypatch.setattr(serve, "_translate_one_chunk", fake_translate_chunk)
    monkeypatch.setattr(serve, "split_pdf_chunk", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(serve, "merge_pdfs", fake_merge_pdfs)
    monkeypatch.setattr(serve, "ensure_book_glossary", lambda source, job_dir, lang: job_dir / "glossary.csv")

    manifest = BookJobManifest.new(
        job_id="job",
        source_pdf_path=tmp_path / "book.pdf",
        job_dir=tmp_path / "job",
        chunk_size=50,
        total_pages=100,
    )
    manifest.chunks[0].status = "done"
    manifest.chunks[0].mono_pdf_path = str(tmp_path / "chunk-1.mono.pdf")
    Path(manifest.chunks[0].mono_pdf_path).write_bytes(b"pdf")
    save_manifest(manifest)

    asyncio.run(serve._run_chunked_manifest(manifest, "en", "zh", None, None))

    assert calls == [2]
    assert manifest.status == "done"
    assert manifest.final_mono_pdf_path.exists()
    assert not (manifest.job_dir / "chunks").exists()

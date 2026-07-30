from pathlib import Path

from fastapi.testclient import TestClient


def test_api_translate_starts_chunked_task_with_reuse_policy(tmp_path: Path, monkeypatch):
    import serve

    uploads = tmp_path / "uploads"
    outputs = tmp_path / "outputs"
    uploads.mkdir()
    outputs.mkdir()
    existing = uploads / "book.pdf"
    existing.write_bytes(b"%PDF-1.4\n")

    started = {}

    async def fake_run_chunked_translation(
        task,
        lang_in,
        lang_out,
        model_override=None,
        chunk_size=50,
    ):
        started["pdf_path"] = task.pdf_path
        started["output_dir"] = task.output_dir
        started["lang_in"] = lang_in
        started["lang_out"] = lang_out
        started["chunk_size"] = chunk_size
        task.status = "done"

    monkeypatch.setattr(serve, "UPLOAD_DIR", uploads)
    monkeypatch.setattr(serve, "OUTPUT_DIR", outputs)
    monkeypatch.setattr(serve, "run_chunked_translation", fake_run_chunked_translation)
    monkeypatch.setattr(serve, "count_pages", lambda _path: 2)

    client = TestClient(serve.app)
    response = client.post(
        "/api/translate",
        data={
            "lang_in": "en",
            "lang_out": "zh",
            "chunked": "true",
            "chunk_size": "1",
            "upload_policy": "reuse",
        },
        files={"file": ("book.pdf", b"new upload ignored", "application/pdf")},
    )

    assert response.status_code == 200
    assert started["pdf_path"] == existing
    assert started["lang_in"] == "en"
    assert started["lang_out"] == "zh"
    assert started["chunk_size"] == 1
    assert started["output_dir"].parent == outputs / "jobs"
    assert existing.read_bytes() == b"%PDF-1.4\n"

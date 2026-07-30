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


def test_upload_filename_is_sanitized(tmp_path: Path):
    decision = resolve_upload_path(tmp_path, "../../bad name.pdf", "reuse")

    assert decision.path == tmp_path / "bad_name.pdf"
    assert decision.should_write is True

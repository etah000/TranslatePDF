from pathlib import Path

from floris.glossary import ensure_book_glossary
from floris.glossary import load_babeldoc_glossary


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


def test_load_babeldoc_glossary_returns_none_for_empty_header(tmp_path: Path):
    glossary = tmp_path / "glossary.csv"
    glossary.write_text("source,target,tgt_lng\n", encoding="utf-8")

    assert load_babeldoc_glossary(glossary, "zh") is None

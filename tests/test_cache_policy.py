from pathlib import Path


def test_babeldoc_cache_row_cap_is_100000():
    text = Path("/data/opensource/BabelDOC/babeldoc/translator/cache.py").read_text(
        encoding="utf-8"
    )

    assert "MAX_CACHE_ROWS = 100_000" in text

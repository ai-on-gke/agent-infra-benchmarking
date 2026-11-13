from textkit.wrap import truncate, wrap


def test_truncate_short_text():
    assert truncate("short", 10) == "short"


def test_truncate_fits_width():
    assert truncate("a long sentence", 10) == "a long ..."


def test_wrap():
    assert wrap("the quick brown fox", 10) == ["the quick", "brown fox"]

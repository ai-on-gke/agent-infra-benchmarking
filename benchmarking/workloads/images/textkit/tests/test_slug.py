from textkit.slug import slugify


def test_slugify_joins_words():
    assert slugify("hello world") == "hello-world"


def test_slugify_drops_punctuation():
    assert slugify("hello, world!") == "hello-world"


def test_slugify_lowercases():
    assert slugify("Hello World") == "hello-world"

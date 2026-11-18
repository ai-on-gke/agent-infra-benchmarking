from textkit.case import camel_to_snake, snake_to_camel


def test_snake_to_camel():
    assert snake_to_camel("max_line_width") == "maxLineWidth"


def test_snake_to_camel_single_word():
    assert snake_to_camel("width") == "width"


def test_camel_to_snake():
    assert camel_to_snake("maxLineWidth") == "max_line_width"

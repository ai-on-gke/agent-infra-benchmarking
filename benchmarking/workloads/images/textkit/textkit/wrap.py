"""Fitting text into a width."""


def truncate(text, width, suffix="..."):
    """text cut to at most width characters, suffix included."""
    if len(text) <= width:
        return text
    return text[:width] + suffix


def wrap(text, width):
    """text's words in lines of at most width characters."""
    lines, line = [], ""
    for word in text.split():
        if line and len(line) + 1 + len(word) > width:
            lines.append(line)
            line = word
        else:
            line = f"{line} {word}" if line else word
    if line:
        lines.append(line)
    return lines

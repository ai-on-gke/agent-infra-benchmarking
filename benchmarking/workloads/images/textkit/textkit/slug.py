"""URL slugs."""

import re


def slugify(text):
    """Lower-cases text and joins its words with hyphens."""
    words = re.findall(r"[A-Za-z0-9]+", text)
    return "-".join(words)

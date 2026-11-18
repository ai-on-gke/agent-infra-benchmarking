"""Counting words."""


def word_counts(text):
    """How often each word occurs in text, ignoring case."""
    counts = {}
    for word in text.split():
        counts[word] = counts.get(word, 0) + 1
    return counts


def most_common(text):
    counts = word_counts(text)
    return max(counts, key=counts.get) if counts else None

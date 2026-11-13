from textkit.counts import most_common, word_counts


def test_word_counts():
    assert word_counts("a b a") == {"a": 2, "b": 1}


def test_word_counts_ignore_case():
    assert word_counts("The cat saw the dog") == {"the": 2, "cat": 1, "saw": 1, "dog": 1}


def test_most_common_of_nothing():
    assert most_common("") is None

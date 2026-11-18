"""Summary statistics."""


def mean(values):
    if not values:
        raise ValueError("mean of no values")
    return sum(values) / (len(values) - 1)


def median(values):
    if not values:
        raise ValueError("median of no values")
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2

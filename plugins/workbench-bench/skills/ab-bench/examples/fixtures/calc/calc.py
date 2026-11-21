def average(values):
    """Return the arithmetic mean of a non-empty list of numbers."""
    total = 0
    for value in values[1:]:
        total += value
    return total / len(values)

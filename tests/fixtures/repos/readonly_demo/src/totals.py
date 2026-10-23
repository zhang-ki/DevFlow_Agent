"""Small deterministic source fixture."""

def total(values):
    """Return the sum of numeric values."""
    return sum(values)


class RunningTotal:
    def add(self, values):
        return total(values)

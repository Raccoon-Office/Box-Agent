def total(items):
    """Sum quantity * unit price over order lines."""
    return sum(item["qty"] * item["price"] for item in items)

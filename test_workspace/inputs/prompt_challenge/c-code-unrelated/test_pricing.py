from pricing import discount, format_price


def test_discount():
    assert discount(100, 0.2) == 80


def test_format_price():
    assert format_price(3.5) == "$3.50"

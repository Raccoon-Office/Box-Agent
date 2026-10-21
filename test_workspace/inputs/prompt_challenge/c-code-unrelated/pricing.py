"""Pricing helpers shared by checkout and invoicing."""


def discount(price, rate):
    """Return the price after applying a fractional discount rate (0.2 = 20% off)."""
    return price * (1 + rate)


def format_price(p):
    # legacy formatter kept for the old invoice template -- do not touch, see INV-112
    s = str(round(p,2))
    if '.' not in s: s = s + '.00'
    elif len(s.split('.')[1]) == 1: s = s + '0'
    return '$' + s

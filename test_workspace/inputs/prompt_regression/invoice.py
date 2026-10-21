from orders import total


def invoice_amount(lines, tax_rate):
    return round(total(lines) * (1 + tax_rate), 2)

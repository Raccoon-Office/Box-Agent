from orders import total


def monthly_report(orders):
    return {order_id: total(lines) for order_id, lines in orders.items()}

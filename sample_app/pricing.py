import tinyfmt


def share(total, people):
    # Planted bug for the lab: people=0 crashes with ZeroDivisionError.
    return tinyfmt.money(total / people)

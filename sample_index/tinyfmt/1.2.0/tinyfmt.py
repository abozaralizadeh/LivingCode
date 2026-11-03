__version__ = "1.2.0"


def money(value):
    return "${:,.2f}".format(float(value))

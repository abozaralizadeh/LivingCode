__version__ = "1.1.0"


def money(value):
    return "${:.2f}".format(float(value))

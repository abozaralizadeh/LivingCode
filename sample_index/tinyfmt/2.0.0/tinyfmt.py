__version__ = "2.0.0"


def money(value):
    # Breaking change for the lab: returns euros, so the sample's own test fails.
    return "EUR {:.2f}".format(float(value))

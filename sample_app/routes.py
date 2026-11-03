"""Request handling for the sample app, with no sockets, so tests can run in a sandbox."""
import health
import pricing


def handle(path, query):
    if path == "/health":
        return health.status()
    if path == "/":
        return 200, "<h1>Sample shop</h1>"
    if path == "/share":
        total = float(query.get("total", "0"))
        people = int(query.get("people", "1"))
        return 200, pricing.share(total, people)
    if path == "/checkout":
        return 200, "checkout"
    return 404, "not found"

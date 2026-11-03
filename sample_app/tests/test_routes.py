import unittest

import pricing
import routes


class RouteTests(unittest.TestCase):
    def test_health(self):
        self.assertEqual(routes.handle("/health", {}), (200, "ok"))

    def test_home(self):
        self.assertEqual(routes.handle("/", {})[0], 200)

    def test_share(self):
        self.assertEqual(pricing.share(10, 2), "$5.00")

    def test_unknown(self):
        self.assertEqual(routes.handle("/nope", {})[0], 404)


if __name__ == "__main__":
    unittest.main()

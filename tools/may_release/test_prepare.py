import unittest

from prepare import PrepareError, exact_sha, pins


class PrepareTests(unittest.TestCase):
    def test_exact_sha(self):
        self.assertEqual(exact_sha("a" * 40), "a" * 40)
        for value in ("A" * 40, "abc", "a" * 39 + "/"):
            with self.assertRaises(PrepareError):
                exact_sha(value)

    def test_cross_platform_pin_must_match(self):
        project = ("packages:\n  MayDesignSystem:\n"
                   "    url: https://github.com/May-Assistant/may-design-system\n"
                   "    revision: " + "a" * 40 + "\n")
        lock = "repository=https://github.com/May-Assistant/may-design-system\nrevision=" + "a" * 40
        self.assertEqual(pins(project, lock)[0], "a" * 40)
        with self.assertRaises(PrepareError):
            pins(project, lock.replace("a" * 40, "b" * 40))
        with self.assertRaises(PrepareError):
            pins(project, lock.replace("May-Assistant", "Somewhere-Else"))


if __name__ == "__main__":
    unittest.main()

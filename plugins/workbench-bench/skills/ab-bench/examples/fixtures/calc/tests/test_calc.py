import unittest

from calc import average


class AverageTest(unittest.TestCase):
    def test_several_values(self):
        self.assertEqual(average([2, 4, 6]), 4)

    def test_single_value(self):
        self.assertEqual(average([5]), 5)


if __name__ == "__main__":
    unittest.main()

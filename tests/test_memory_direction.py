"""Regression checks for institutional-memory directional evidence.

Run: python -m unittest tests/test_memory_direction.py
"""
import unittest
from types import SimpleNamespace

from firm.memory_rag import Hit, precedent_signal


def hit(direction, excess, score=1.0, visible=True, label="right"):
    memo = SimpleNamespace(direction=direction, excess_20d=excess, label=label)
    return Hit(memo=memo, score=score, bm25_rank=1, dense_rank=1,
               outcome_visible=visible)


class DirectionalMemoryTests(unittest.TestCase):
    def test_buy_ignores_sell_precedents(self):
        result = precedent_signal([hit(1, 4), hit(-1, -90)], 1)
        self.assertEqual(result["n"], 1)
        self.assertEqual(result["stance"], "supports")
        self.assertEqual(result["aligned"], 4.0)

    def test_sell_ignores_buy_precedents(self):
        result = precedent_signal([hit(-1, -6), hit(1, 80)], -1)
        self.assertEqual(result["n"], 1)
        self.assertEqual(result["stance"], "supports")
        self.assertEqual(result["aligned"], 6.0)

    def test_immature_outcomes_excluded(self):
        result = precedent_signal([hit(1, 20, visible=False)], 1)
        self.assertEqual(result["n"], 0)
        self.assertEqual(result["stance"], "no memory")

    def test_neutral_direction_has_no_signal(self):
        result = precedent_signal([hit(1, 20)], 0)
        self.assertEqual(result["n"], 0)

    def test_no_matching_direction(self):
        result = precedent_signal([hit(-1, -5)], 1)
        self.assertEqual(result["n"], 0)


if __name__ == "__main__":
    unittest.main()

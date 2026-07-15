"""Independent stage→part ranges (no growing re-concat)."""

from __future__ import annotations

import unittest

from import_70mai import plan_stage_batches


class StageBatchPlanTests(unittest.TestCase):
    def test_empty(self) -> None:
        self.assertEqual(plan_stage_batches(0, 10), [])

    def test_single_batch(self) -> None:
        self.assertEqual(plan_stage_batches(7, 10), [(0, 7)])

    def test_exact_batches(self) -> None:
        self.assertEqual(
            plan_stage_batches(20, 10),
            [(0, 10), (10, 20)],
        )

    def test_tail_batch(self) -> None:
        self.assertEqual(
            plan_stage_batches(248, 10),
            [(i, min(i + 10, 248)) for i in range(0, 248, 10)],
        )
        ranges = plan_stage_batches(248, 10)
        self.assertEqual(len(ranges), 25)
        self.assertEqual(ranges[-1], (240, 248))
        # Parts are independent index ranges — never overlapping / nested.
        covered = 0
        prev_end = 0
        for start, end in ranges:
            self.assertEqual(start, prev_end)
            self.assertGreater(end, start)
            covered += end - start
            prev_end = end
        self.assertEqual(covered, 248)

    def test_batch_size_floor(self) -> None:
        self.assertEqual(plan_stage_batches(3, 0), [(0, 1), (1, 2), (2, 3)])


if __name__ == "__main__":
    unittest.main()

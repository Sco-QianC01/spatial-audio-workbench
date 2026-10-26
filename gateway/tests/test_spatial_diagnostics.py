"""Diagnostics describe audible risks, without pretending to predict mix quality."""
from pathlib import Path
import sys
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from spatial_diagnostics import analyze_relationships, stereo_windows

RATE = 44100


def tone(frequency=440, seconds=2, amplitude=.1):
    t = np.arange(round(seconds * RATE)) / RATE
    x = amplitude * np.sin(2 * np.pi * frequency * t)
    return np.column_stack((x, x)).astype("float32")


class DiagnosticsTests(unittest.TestCase):
    def test_brief_antiphase_is_not_hidden_by_global_correlation(self):
        data = tone(seconds=4)
        data[RATE:2*RATE, 1] *= -1
        self.assertGreater(np.corrcoef(data.T)[0, 1], .4)
        result = stereo_windows(data, RATE)
        self.assertEqual(result["windowSeconds"], .5)
        flagged = [v for v in result["windows"] if v["monoRisk"]]
        self.assertEqual([v["startSeconds"] for v in flagged], [1, 1.5])
        self.assertTrue(all(v["monoFullyCancelled"] for v in flagged))
        self.assertEqual(result["riskDurationSeconds"], 1)

    def test_silence_is_unknown_not_good_stereo_or_valid_overlap(self):
        quiet = np.zeros((RATE, 2), dtype="float32")
        result = stereo_windows(quiet, RATE)
        self.assertTrue(all(v["correlation"] is None for v in result["windows"]))
        report = analyze_relationships({"drums": quiet, "bass": quiet}, RATE)
        self.assertEqual(report["overlapPairs"], [])

    def test_overlap_requires_same_band_time_and_relevant_level(self):
        a, b = tone(220), tone(220, amplitude=.08)
        report = analyze_relationships({"drums": a, "bass": b}, RATE)
        self.assertEqual(len(report["overlapPairs"]), 1)
        self.assertIn("120–250 Hz", report["overlapPairs"][0]["bands"])
        self.assertIn("not a psychoacoustic masking", report["method"])
        report = analyze_relationships({"drums": a, "bass": tone(6000)}, RATE)
        self.assertEqual(report["overlapPairs"], [])
        report = analyze_relationships({"drums": a, "bass": b * 1e-4}, RATE)
        self.assertEqual(report["overlapPairs"], [])

    def test_disjoint_events_do_not_generate_simultaneous_overlap(self):
        a = tone(seconds=4)
        b = a.copy()
        a[2*RATE:] = 0
        b[:2*RATE] = 0
        report = analyze_relationships({"drums": a, "bass": b}, RATE)
        self.assertEqual(report["overlapPairs"], [])


if __name__ == "__main__":
    unittest.main()

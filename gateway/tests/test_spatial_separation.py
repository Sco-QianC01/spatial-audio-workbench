"""Overlap/add tests use a fake model and do not establish separation quality."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import soundfile as sf
import torch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import spatial_separation as separation


class ScaledChannels:
    def __call__(self, audio):
        return torch.stack([audio * factor for factor in (0.25, -0.5, 2.0, 1.0)], dim=1)


class SeparationTests(unittest.TestCase):
    def test_overlap_preserves_gain_channels_boundaries_and_short_tail(self):
        for frames in (1, 7, 31, 32, 33, 48, 55, 56, 57, 103):
            for overlap in (0, 1, 8, 23):
                audio = np.column_stack((np.arange(frames) / 8 + 1,
                                         -np.arange(frames) / 3 - 2)).astype(np.float32)
                audio[-1] = (11, -17)
                blocks = list(separation.separated_blocks(
                    ScaledChannels(), lambda start, count: audio[start:start + count],
                    frames, chunk_frames=32, overlap_frames=overlap))
                actual = np.concatenate(blocks, axis=-1)
                expected = np.stack([audio.T * factor for factor in (0.25, -0.5, 2.0, 1.0)])
                with self.subTest(frames=frames, overlap=overlap):
                    self.assertEqual(actual.shape, (4, 2, frames))
                    np.testing.assert_allclose(actual, expected, rtol=2e-6, atol=2e-6)

    def test_rejects_invalid_model_results(self):
        audio = np.ones((20, 2), dtype=np.float32)
        for model in (lambda batch: torch.zeros(1, 3, 2, 32),
                      lambda batch: torch.full((1, 4, 2, 32), float("nan"))):
            with self.assertRaises(ValueError):
                list(separation.separated_blocks(model, lambda start, count: audio[start:start + count],
                                                 20, 32, 8))

    def test_rejects_bad_overlap_and_reader(self):
        for overlap in (-1, 32):
            with self.assertRaises(ValueError):
                list(separation.separated_blocks(ScaledChannels(), lambda *_: None, 20, 32, overlap))
        with self.assertRaises(ValueError):
            list(separation.separated_blocks(ScaledChannels(), lambda *_: np.zeros((20, 1)),
                                             20, 32, 8))

    def test_writes_float_stems_same_length_without_normalizing_and_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path = root / "input.wav"
            weights = root / "weights.pt"
            weights.write_bytes(b"test fixture")
            audio = np.column_stack((np.full(57, 0.75), np.full(57, -0.25))).astype(np.float32)
            sf.write(input_path, audio, 44100, subtype="FLOAT")
            identity = {"name": "fake-only-for-test", "strictLoad": True, "tensorCount": 0}
            with patch.object(separation, "load_model", return_value=(ScaledChannels(), identity)):
                report = separation.separate(input_path, root / "output", weights)
            for source, factor in zip(separation.SOURCES, (0.25, -0.5, 2.0, 1.0)):
                actual, rate = sf.read(root / "output" / f"{source}.wav", dtype="float32")
                np.testing.assert_allclose(actual, audio * factor)
                self.assertEqual(rate, 44100)
                self.assertEqual(sf.info(root / "output" / f"{source}.wav").subtype, "FLOAT")
                self.assertTrue(report["outputs"][source]["finite"])
            saved = json.loads((root / "output" / "separation.json").read_text())
            self.assertEqual(saved["normalization"], "none")
            self.assertEqual(saved["input"]["frames"], 57)
            self.assertAlmostEqual(saved["reconstruction"]["relativeRms"], 1.75, places=6)
            self.assertAlmostEqual(saved["reconstruction"]["residualPeak"], 0.75 * 1.75, places=6)
            self.assertFalse(saved["reconstruction"]["residualAddedToStems"])
            self.assertAlmostEqual(saved["outputs"]["other"]["peak"], 1.5, places=6)
            with self.assertRaises(ValueError):
                separation.separate(input_path, root / "output", weights)

    def test_input_rejects_mono_wrong_rate_empty_and_nonfinite(self):
        with tempfile.TemporaryDirectory() as directory:
            for index, (audio, rate) in enumerate((
                    (np.ones((8, 1)), 44100), (np.ones((8, 2)), 48000),
                    (np.empty((0, 2)), 44100), (np.full((8, 2), np.nan), 44100))):
                path = Path(directory) / f"{index}.wav"
                sf.write(path, audio, rate, subtype="FLOAT")
                with self.assertRaises(ValueError):
                    separation.inspect_input(path)


if __name__ == "__main__":
    unittest.main()

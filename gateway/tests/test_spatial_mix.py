import copy
import json
import subprocess
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

import numpy as np
import soundfile as sf

from gateway import spatial_mix as mix

FFMPEG = shutil.which('ffmpeg')
if not FFMPEG:
    raise RuntimeError('Spatial mix tests require ffmpeg on PATH; install ffmpeg before running this suite.')


class SpatialMixTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.stems = self.root / 'stems'
        self.stems.mkdir()
        t = np.arange(44100 * 4) / 44100
        self.tone = np.column_stack((np.sin(2*np.pi*1000*t), np.sin(2*np.pi*1000*t))) * .08
        self.original = self.root / 'original.wav'
        sf.write(self.original, self.tone + .003, 44100, subtype='FLOAT')
        for sid in mix.IDS:
            sf.write(self.stems / (sid+'.wav'), self.tone / 4, 44100, subtype='FLOAT')

    def tearDown(self):
        self.temp.cleanup()

    def analyze(self):
        return mix.analyze_project(self.original, self.stems, FFMPEG)

    def test_direct_worker_style_import(self):
        gateway_dir = Path(mix.__file__).parent
        result = subprocess.run([sys.executable, '-c',
                                 "import spatial_mix; assert spatial_mix.IDS[0] == 'drums'"],
                                cwd=gateway_dir, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr.decode())

    def test_measurement_and_silence(self):
        a = self.analyze()
        self.assertEqual(a['schema'], 'spatial-analysis-v1')
        self.assertIn('ffmpeg version', a['original']['measurement']['ffmpegVersion'].lower())
        self.assertEqual(a['original']['measurement']['fields'], ['input_i', 'input_tp', 'input_lra'])
        self.assertIn('短', a['original']['loudnessRangeInterpretation'])
        self.assertFalse(a['original']['loudnessRangeSuitable'])
        self.assertEqual(a['original']['stereoTime']['riskDurationSeconds'], 0)
        self.assertIn('not a psychoacoustic masking', a['relationships']['method'])
        self.assertTrue(-25 < a['original']['integratedLufs'] < -18)
        self.assertTrue(-23 < a['original']['truePeakDbTP'] < -20)
        self.assertAlmostEqual(a['original']['correlation'], 1)
        self.assertGreater(a['reconstructionResidual']['rms'], .002)
        silent = self.root / 'silence.wav'
        sf.write(silent, np.zeros_like(self.tone), 44100, subtype='FLOAT')
        m = mix.measure_audio(silent, FFMPEG)
        self.assertIsNone(m['integratedLufs'])
        self.assertFalse(m['active'])
        json.dumps(a, allow_nan=False)

    def test_short_nonzero_without_true_peak_is_rejected_but_zero_is_allowed(self):
        short = self.root / 'short.wav'
        sf.write(short, np.full((10, 2), 2, dtype=np.float32), 44100, subtype='FLOAT')
        with self.assertRaisesRegex(ValueError, '真峰'):
            mix.measure_audio(short, FFMPEG)
        for sid in mix.IDS:
            sf.write(self.stems/(sid+'.wav'), np.full((10, 2), .5), 44100, subtype='FLOAT')
        with self.assertRaisesRegex(ValueError, '真峰'):
            mix.analyze_project(short, self.stems, FFMPEG)
        sf.write(short, np.zeros((10, 2)), 44100, subtype='FLOAT')
        measured = mix.measure_audio(short, FFMPEG)
        self.assertEqual(measured['rms'], 0)
        self.assertIsNone(measured['truePeakDbTP'])

    def test_tenth_second_true_peak_remains_measurable_without_valid_loudness(self):
        short = self.root / 'tenth-second.wav'
        sf.write(short, self.tone[:4410], 44100, subtype='FLOAT')
        measured = mix.measure_audio(short, FFMPEG)
        self.assertEqual(measured['frames'], 4410)
        self.assertIsNotNone(measured['truePeakDbTP'])
        self.assertIsNone(measured['integratedLufs'])
        for sid in mix.IDS:
            sf.write(self.stems/(sid+'.wav'), self.tone[:4410]/4, 44100, subtype='FLOAT')
        analysis = mix.analyze_project(short, self.stems, FFMPEG)
        result = mix.process_mix(short, self.stems, mix.build_plan(analysis, 'preserve'),
                                 self.root/'tenth-second-result', FFMPEG, analysis)
        self.assertEqual(result['loudnessMatch']['status'], 'unavailable')
        self.assertIsNotNone(result['metrics']['candidate']['truePeakDbTP'])
        np.testing.assert_allclose(sf.read(short)[0],
                                   sf.read(self.root/'tenth-second-result/candidate.wav')[0],
                                   atol=1e-7)
        unavailable = {'integratedLufs': None, 'truePeakDbTP': None, 'loudnessRangeLu': None}
        with patch.object(mix, 'parse_loudness', return_value=unavailable):
            with self.assertRaisesRegex(ValueError, '真峰'):
                mix.measure_audio(short, FFMPEG)

    def test_missing_true_peak_cannot_enter_plan_or_pass_final_render_guard(self):
        analysis = self.analyze()
        invalid = copy.deepcopy(analysis)
        invalid['original']['truePeakDbTP'] = None
        with self.assertRaisesRegex(ValueError, '真峰'):
            mix.build_plan(invalid, 'preserve')
        plan = mix.build_plan(analysis, 'preserve')
        real_measure = mix.measure_audio
        candidate_calls = 0

        def missing_final_peak(path, ffmpeg):
            nonlocal candidate_calls
            measurement = real_measure(path, ffmpeg)
            if Path(path).name == 'candidate.wav':
                candidate_calls += 1
                if candidate_calls == 2:
                    measurement['truePeakDbTP'] = None
            return measurement

        with patch.object(mix, 'measure_audio', side_effect=missing_final_peak):
            with self.assertRaisesRegex(ValueError, '真峰'):
                mix.process_mix(self.original, self.stems, plan,
                                self.root/'missing-final-peak', FFMPEG, analysis)

    def test_reject_channels_and_alignment(self):
        sf.write(self.original, np.zeros((44100, 4)), 44100, subtype='FLOAT')
        with self.assertRaises(ValueError):
            self.analyze()

    def test_plan_validation_rejects_injection_nan_bool_duplicates(self):
        a = self.analyze()
        p = mix.build_plan(a, 'preserve')
        mix.validate_plan(p, a)
        for mutate in (lambda q: q.update(tool='shell'),
                       lambda q: q.update(masterGainDb=float('nan')),
                       lambda q: q.update(masterGainDb=True),
                       lambda q: q['stems'][1].update(id='drums'),
                       lambda q: q['stems'][0].update(path='/tmp/evil'),
                       lambda q: q.update(analysisHash='bad')):
            q = copy.deepcopy(p)
            mutate(q)
            with self.assertRaises(ValueError):
                mix.validate_plan(q, a)
        with self.assertRaises(ValueError):
            mix.build_plan(a, 'reference')

    def test_bypass_preserves_residual_and_aligned_matching(self):
        a = self.analyze()
        p = mix.build_plan(a, 'preserve')
        out = self.root / 'result'
        result = mix.process_mix(self.original, self.stems, p, out, FFMPEG)
        original, sr = sf.read(self.original)
        candidate, _ = sf.read(out/'candidate.wav')
        np.testing.assert_allclose(original, candidate, atol=1e-7)
        residual, _ = sf.read(out/'residual.wav')
        np.testing.assert_allclose(residual, .003, atol=1e-8)
        rendered = sum(sf.read(out/('processed_stems/'+sid+'.wav'))[0] for sid in mix.IDS)
        np.testing.assert_allclose(rendered, candidate, atol=1e-7)
        self.assertEqual(result['residualRouting'], 'other')
        self.assertTrue(result['masterAppliedToStems'])
        self.assertEqual(result['status'], 'awaiting-listening')
        self.assertEqual(result['loudnessMatch']['status'], 'matched')
        for filename in ('baseline_matched.wav', 'candidate_matched.wav'):
            self.assertEqual(sf.info(out/filename).frames, len(original))
        for name in ('baseline', 'candidate'):
            stereo = sf.read(out/(name+'_matched.wav'))[0]
            mono = sf.read(out/(name+'_mono.wav'))[0]
            np.testing.assert_allclose(mono[:, 0], stereo.mean(axis=1), atol=1e-7)
            np.testing.assert_array_equal(mono[:, 0], mono[:, 1])
            self.assertIn(name+'_mono.wav', result['outputFiles'])
        json.dumps(result, allow_nan=False)

    def test_clarity_binds_observations_by_id_and_not_instrument_name(self):
        a = {'original': {'truePeakDbTP': -2}, 'stems': [
            {'id': sid, 'active': True, 'energyRatios': {'sub35': .4 if sid == 'drums' else .01}}
            for sid in reversed(mix.IDS)]}
        plan = mix.build_plan(a, 'clarity')
        values = {stem['id']: stem['highpassHz'] for stem in plan['stems']}
        self.assertEqual(values, {'drums': 30, 'bass': 0, 'other': 0, 'vocals': 0})
        a['stems'].reverse()
        self.assertEqual(mix.build_plan(a, 'clarity')['stems'], plan['stems'])

    def test_dsp_filters_and_linked_compression_change_signal(self):
        t = np.arange(44100 * 4) / 44100
        signal = .2*np.sin(2*np.pi*15*t) + .15*np.sin(2*np.pi*1000*t)
        stereo = np.column_stack((signal, signal*.5))
        for sid in mix.IDS:
            sf.write(self.stems/(sid+'.wav'), stereo/4, 44100, subtype='FLOAT')
        sf.write(self.original, stereo, 44100, subtype='FLOAT')
        a = self.analyze()
        p = mix.build_plan(a, 'preserve')
        p['stems'][0]['highpassHz'] = 40
        p['stems'][0]['eq'] = {'frequencyHz': 1000, 'gainDb': -3, 'q': 1}
        p['stems'][0]['compressor'] = {'thresholdDb': -36, 'ratio': 3, 'attackMs': 5, 'releaseMs': 100}
        out = self.root/'filtered'
        mix.process_mix(self.original, self.stems, p, out, FFMPEG)
        changed, _ = sf.read(out/'processed_stems/drums.wav')
        self.assertLess(np.sqrt(np.mean(changed**2)), np.sqrt(np.mean((stereo/4)**2))*.8)
        np.testing.assert_allclose(changed[:, 1], changed[:, 0]*.5, atol=1e-7)
        self.assertEqual(len(changed), len(stereo))

    def test_reference_shelves_and_silence_matching(self):
        reference = self.root/'reference.wav'
        t = np.arange(44100*4)/44100
        ref = self.tone + .1*np.column_stack((np.sin(2*np.pi*8000*t),)*2)
        sf.write(reference, ref, 44100, subtype='FLOAT')
        a = mix.analyze_project(self.original, self.stems, FFMPEG, reference)
        p = mix.build_plan(a, 'reference')
        self.assertEqual(p['masterEq']['highShelfDb'], 3)
        out = self.root/'reference_result'
        r = mix.process_mix(self.original, self.stems, p, out, FFMPEG, analysis=a)
        self.assertGreater(r['metrics']['candidate']['energyRatios']['high'],
                           r['metrics']['baseline']['energyRatios']['high'])
        rendered = sum(sf.read(out/('processed_stems/'+sid+'.wav'))[0] for sid in mix.IDS)
        np.testing.assert_allclose(rendered, sf.read(out/'candidate.wav')[0], atol=2e-7)
        sf.write(self.original, np.zeros_like(self.tone), 44100, subtype='FLOAT')
        for sid in mix.IDS:
            sf.write(self.stems/(sid+'.wav'), np.zeros_like(self.tone), 44100, subtype='FLOAT')
        a = self.analyze()
        r = mix.process_mix(self.original, self.stems, mix.build_plan(a, 'preserve'),
                            self.root/'silent_result', FFMPEG)
        self.assertEqual(r['loudnessMatch']['status'], 'unavailable')
        self.assertIsNone(r['loudnessMatch']['targetLufs'])

    def test_intersample_true_peak_and_headroom(self):
        n = np.arange(44100*4)
        x = .96*np.sin(2*np.pi*.25*n + np.pi/4)
        sf.write(self.original, np.column_stack((x,x)), 44100, subtype='FLOAT')
        for sid in mix.IDS:
            sf.write(self.stems/(sid+'.wav'), np.column_stack((x,x))/4, 44100, subtype='FLOAT')
        a = self.analyze()
        self.assertGreater(a['original']['truePeakDbTP'], a['original']['samplePeakDbFS'] + 1)
        p = mix.build_plan(a, 'preserve')
        out = self.root/'peaks'
        r = mix.process_mix(self.original, self.stems, p, out, FFMPEG)
        self.assertLessEqual(r['metrics']['candidate']['truePeakDbTP'], -1)
        self.assertLess(p['masterGainDb'], 0)
        rendered = sum(sf.read(out/('processed_stems/'+sid+'.wav'))[0] for sid in mix.IDS)
        candidate, _ = sf.read(out/'candidate.wav')
        np.testing.assert_allclose(rendered, candidate, atol=2e-7)


if __name__ == '__main__':
    unittest.main()

import copy
import hashlib
import json
import math
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from spatial_content import build_content_scene


def observation(sid, rms=.1, low=.1, activity=.9, crest=10, cancellation=-6):
    return {'id': sid, 'rms': rms, 'active': rms > .001,
            'energyRatios': {'low': low}, 'activityRatio': activity,
            'crestDb': crest, 'monoCancellationDb': cancellation}


def fixture():
    scene = {'schema': 'spatial-scene-v1', 'objects': [
        {'id': sid, 'azimuthDeg': az, 'elevationDeg': 30, 'widthDeg': 45,
         'gainDb': -3, 'muted': False, 'audioUrl': sid + '.wav',
         'keyframes': [{'timeSeconds': 0, 'azimuthDeg': 0, 'elevationDeg': 0},
                       {'timeSeconds': 10, 'azimuthDeg': 90, 'elevationDeg': 30}]}
        for sid, az in zip(('one', 'two', 'three', 'four'), (100, -100, 60, -60))]}
    analysis = {'original': {'rms': .3}, 'reconstructionResidual': {'rms': .001},
                'stems': [observation('one', .2, .8), observation('two', .18),
                          observation('three', .04), observation('four', .03, activity=.2)]}
    return scene, analysis


class ContentSceneTests(unittest.TestCase):
    def test_spacious_protects_subject_and_expands_mono_companion(self):
        scene, analysis = fixture()
        analysis['stems'][2]['monoCancellationDb'] = 0
        before = copy.deepcopy((scene, analysis))
        candidate, report = build_content_scene(scene, analysis, layout='spacious')
        self.assertEqual((scene, analysis), before)
        self.assertEqual(report['expandedObjectCount'], 1)
        self.assertEqual(candidate['objects'][0]['elevationDeg'], 0)
        self.assertEqual(candidate['objects'][1]['elevationDeg'], 0)
        self.assertEqual(candidate['objects'][1]['widthDeg'], 60)
        self.assertEqual((candidate['objects'][2]['azimuthDeg'],
                          candidate['objects'][2]['elevationDeg'],
                          candidate['objects'][2]['widthDeg']), (115, 30, 75))
        self.assertEqual(candidate['objects'][3], scene['objects'][3])
        for original, result in zip(scene['objects'], candidate['objects']):
            for key in ('id', 'gainDb', 'muted', 'audioUrl'):
                self.assertEqual(result[key], original[key])
        self.assertTrue(all(not item['keyframes'] for item in candidate['objects'][:3]))

    def test_spacious_no_safe_companion_reports_no_expansion(self):
        scene, analysis = fixture()
        scene['objects'][2]['muted'] = True
        candidate, report = build_content_scene(scene, analysis, layout='spacious')
        self.assertEqual(report['expandedObjectCount'], 0)
        self.assertTrue(any('No safe' in warning for warning in report['warnings']))
        self.assertEqual(candidate['objects'][2], scene['objects'][2])

    def test_spacious_residual_is_front_and_quiet_invalid_are_unchanged(self):
        scene, analysis = fixture()
        analysis['reconstructionResidual']['rms'] = .06
        candidate, report = build_content_scene(scene, analysis, 'three', 'spacious')
        self.assertEqual(report['expandedObjectCount'], 0)
        self.assertEqual((candidate['objects'][2]['azimuthDeg'],
                          candidate['objects'][2]['elevationDeg']), (0, 0))
        for change in ('quiet', 'missing', 'transient'):
            current = copy.deepcopy(analysis)
            current['reconstructionResidual']['rms'] = 0
            if change == 'quiet':
                current['stems'][2]['rms'] = .0001
            elif change == 'missing':
                del current['stems'][2]['crestDb']
            else:
                current['stems'][2]['activityRatio'] = .1
            result, _ = build_content_scene(scene, current, layout='spacious')
            self.assertEqual(result['objects'][2], scene['objects'][2])

    def test_observed_roles_and_no_mutation(self):
        scene, analysis = fixture()
        before = copy.deepcopy((scene, analysis))
        candidate, report = build_content_scene(scene, analysis)
        self.assertEqual((scene, analysis), before)
        self.assertEqual(report['schema'], 'spatial-content-v1')
        encoded = json.dumps(analysis, sort_keys=True, ensure_ascii=False,
                             separators=(',', ':'), allow_nan=False).encode()
        self.assertEqual(report['analysisHash'], hashlib.sha256(encoded).hexdigest())
        self.assertEqual([o['role'] for o in report['objects']],
                         ['front-low', 'front-primary', 'surround-secondary', 'unchanged'])
        for original, result in zip(scene['objects'], candidate['objects']):
            for key in ('id', 'gainDb', 'muted', 'audioUrl'):
                self.assertEqual(result[key], original[key])
        self.assertEqual(candidate['objects'][-1], scene['objects'][-1])
        self.assertTrue(all(not o['keyframes'] for o in candidate['objects'][:3]))
        json.dumps((candidate, report), allow_nan=False)
        for obj in candidate['objects']:
            for key, low, high in (('azimuthDeg', -180, 180), ('elevationDeg', -90, 90), ('widthDeg', 0, 120)):
                self.assertTrue(math.isfinite(obj[key]) and low <= obj[key] <= high)

    def test_id_renaming_and_observation_order(self):
        scene, analysis = fixture()
        expected_scene, expected_report = build_content_scene(scene, analysis)
        analysis['stems'].reverse()
        actual_scene, actual_report = build_content_scene(scene, analysis)
        self.assertEqual(actual_scene, expected_scene)
        self.assertEqual(actual_report['objects'], expected_report['objects'])
        rename = dict(zip(('one', 'two', 'three', 'four'), ('vocals', 'bass', 'drums', 'other')))
        for obj in scene['objects']:
            obj['id'] = rename[obj['id']]
        for obs in analysis['stems']:
            obs['id'] = rename[obs['id']]
        _, renamed_report = build_content_scene(scene, analysis)
        self.assertEqual([o['role'] for o in renamed_report['objects']],
                         [o['role'] for o in expected_report['objects']])

    def test_quiet_missing_duplicate_and_nonfinite_are_conservative(self):
        for change in ('quiet', 'missing', 'duplicate', 'nan', 'inf', 'bool'):
            with self.subTest(change=change):
                scene, analysis = fixture()
                if change == 'quiet':
                    analysis['stems'][0]['rms'] = 0
                elif change == 'missing':
                    del analysis['stems'][0]['activityRatio']
                elif change == 'duplicate':
                    analysis['stems'].append(copy.deepcopy(analysis['stems'][0]))
                else:
                    analysis['stems'][0]['rms'] = {'nan': float('nan'), 'inf': float('inf'), 'bool': True}[change]
                candidate, report = build_content_scene(scene, analysis)
                self.assertEqual(candidate['objects'][0], scene['objects'][0])
                self.assertEqual(report['objects'][0]['role'], 'unchanged')
                json.dumps(report, allow_nan=False)
                if change in ('nan', 'inf'):
                    self.assertIsNone(report['analysisHash'])
                    self.assertTrue(report['warnings'])

    def test_side_width_and_complete_cancellation(self):
        scene, analysis = fixture()
        scene['objects'][0]['widthDeg'] = 0
        analysis['stems'][0].update(monoFullyCancelled=True, monoCancellationDb=None)
        candidate, report = build_content_scene(scene, analysis)
        self.assertGreater(candidate['objects'][0]['widthDeg'], 0)
        self.assertEqual(report['objects'][0]['evidence']['sideEnergyRatio'], 1)
        self.assertTrue(report['objects'][0]['warnings'])
        self.assertEqual(candidate['objects'][2]['widthDeg'], scene['objects'][2]['widthDeg'])

    def test_side_protection_retains_unclassified_position_and_motion(self):
        scene, analysis = fixture()
        scene['objects'][3]['widthDeg'] = 0
        candidate, report = build_content_scene(scene, analysis)
        expected = copy.deepcopy(scene['objects'][3])
        expected['widthDeg'] = 30
        self.assertEqual(candidate['objects'][3], expected)
        self.assertEqual(report['objects'][3]['role'], 'unchanged')
        self.assertTrue(any('width only' in r for r in report['objects'][3]['reasons']))

    def test_muted_strong_stem_does_not_set_energy_reference(self):
        scene, analysis = fixture()
        scene['objects'][0]['muted'] = True
        analysis['stems'][0]['rms'] = .9
        analysis['reconstructionResidual']['rms'] = .1
        candidate, report = build_content_scene(scene, analysis, 'one')
        self.assertEqual(candidate['objects'][0], scene['objects'][0])
        self.assertEqual(report['objects'][0]['role'], 'unchanged')
        self.assertEqual(report['objects'][0]['evidence']['audibleRms'], 0)
        self.assertEqual(report['objects'][1]['role'], 'front-primary')

    def test_scene_gain_changes_energy_roles_without_changing_raw_rms(self):
        scene, analysis = fixture()
        analysis['stems'][0]['energyRatios']['low'] = .1
        scene['objects'][0]['gainDb'] = -24
        candidate, report = build_content_scene(scene, analysis)
        self.assertEqual(report['objects'][0]['role'], 'surround-secondary')
        self.assertEqual(report['objects'][1]['role'], 'front-primary')
        self.assertEqual(report['objects'][0]['evidence']['rms'], .2)
        self.assertAlmostEqual(report['objects'][0]['evidence']['audibleRms'], .2 * 10 ** (-24 / 20))
        self.assertEqual(candidate['objects'][0]['gainDb'], -24)

    def test_residual_carrier_requires_explicit_routing(self):
        scene, analysis = fixture()
        analysis['reconstructionResidual']['rms'] = .06
        candidate, report = build_content_scene(scene, analysis, 'three')
        carrier = candidate['objects'][2]
        self.assertEqual((carrier['azimuthDeg'], carrier['elevationDeg'], carrier['keyframes']), (0, 0, []))
        self.assertEqual(report['objects'][2]['role'], 'residual-carrier')
        _, unrouted = build_content_scene(scene, analysis)
        self.assertEqual(unrouted['objects'][2]['role'], 'surround-secondary')
        self.assertTrue(unrouted['warnings'])
        for raw_observation in ('quiet', 'missing'):
            with self.subTest(raw_observation=raw_observation):
                analysis['stems'][2] = (observation('three', rms=0)
                                        if raw_observation == 'quiet' else {'id': 'three'})
                scene['objects'][2]['widthDeg'] = 0
                protected, protected_report = build_content_scene(scene, analysis, 'three')
                carrier = protected['objects'][2]
                self.assertEqual((carrier['azimuthDeg'], carrier['elevationDeg'], carrier['keyframes']), (0, 0, []))
                self.assertGreaterEqual(carrier['widthDeg'], 30)
                self.assertEqual(protected_report['objects'][2]['role'], 'residual-carrier')
                unrouted, unrouted_report = build_content_scene(scene, analysis)
                self.assertEqual(unrouted['objects'][2], scene['objects'][2])
                self.assertEqual(unrouted_report['objects'][2]['role'], 'unchanged')
                json.dumps(protected_report, allow_nan=False)

    def test_invalid_scene_coordinate_rejected(self):
        scene, analysis = fixture()
        scene['objects'][0]['widthDeg'] = float('nan')
        with self.assertRaises(ValueError):
            build_content_scene(scene, analysis)

    def test_extreme_scene_scaled_rms_is_unknown_and_finite(self):
        scene, analysis = fixture()
        scene['objects'][0]['gainDb'] = 6
        analysis['stems'][0]['rms'] = 1e308
        candidate, report = build_content_scene(scene, analysis)
        self.assertEqual(candidate['objects'][0], scene['objects'][0])
        self.assertIsNone(report['objects'][0]['evidence']['audibleRms'])
        json.dumps(report, allow_nan=False)

    def test_extreme_residual_ratio_does_not_emit_nonfinite(self):
        scene, analysis = fixture()
        analysis['original']['rms'] = 1e-308
        analysis['reconstructionResidual']['rms'] = 1e308
        _, report = build_content_scene(scene, analysis, 'three')
        self.assertIsNone(report['residualRmsRatio'])
        self.assertTrue(report['warnings'])
        json.dumps(report, allow_nan=False)


if __name__ == '__main__':
    unittest.main()

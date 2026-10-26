"""Native ACE objects remain individually routable, with no fullmix residual repair."""
import copy
import json
import shutil
from pathlib import Path
import tempfile
import unittest

import numpy as np
import soundfile as sf

from gateway import spatial_mix as mix
from gateway.spatial_audio import (IDS, NATIVE_IDS, NATIVE_SCHEMA, default_scene,
                                   project_stem_ids, validate_scene)

FFMPEG = shutil.which('ffmpeg')
if not FFMPEG:
    raise RuntimeError('Native stem tests require ffmpeg on PATH; install ffmpeg before running this suite.')


class NativeStemTests(unittest.TestCase):
    def project(self, count):
        return {'id': 'a'*32, 'input': {'durationSeconds': 1},
                'nativeStems': {'schema': NATIVE_SCHEMA,
                                'tracks': [{'id': sid} for sid in NATIVE_IDS[:count]]}}

    def test_variable_scene_and_legacy_defaults(self):
        legacy = {'id': 'b'*32, 'input': {'durationSeconds': 1}}
        self.assertEqual(project_stem_ids(legacy), IDS)
        self.assertNotIn('speakerLayout', default_scene(legacy))
        self.assertEqual([o['azimuthDeg'] for o in default_scene(legacy)['objects']], [-35, 0, 100, 25])
        for count in (2, 12):
            project = self.project(count)
            scene = default_scene(project)
            self.assertEqual(scene, validate_scene(scene, project))
            self.assertEqual(scene['speakerLayout'], '7.1.4')
            np.testing.assert_allclose([o['azimuthDeg'] for o in scene['objects']], np.linspace(-30, 30, count))
            for i, obj in enumerate(scene['objects']):
                self.assertEqual(obj['label'], f'轨道{i+1:02d}')
                self.assertEqual((obj['elevationDeg'], obj['widthDeg'], obj['keyframes']), (0, 30, []))
            invalid = copy.deepcopy(scene)
            invalid['objects'][-1]['id'] = invalid['objects'][0]['id']
            with self.assertRaises(ValueError):
                validate_scene(invalid, project)
        for tracks in ([{'id': 'vocals'}], [{'id': 'other'}, {'id': 'vocals'}],
                       [{'id': 'vocals'}, {'id': 'vocals'}]):
            project = self.project(2)
            project['nativeStems']['tracks'] = tracks
            with self.assertRaises(ValueError):
                project_stem_ids(project)

    def test_native_two_and_twelve_track_analysis_plan_and_render(self):
        for count in (2, 12):
            with self.subTest(count=count), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                stems = root/'stems'
                stems.mkdir()
                metadata = self.project(count)['nativeStems']
                (stems/'separation.json').write_text(json.dumps(metadata))
                t = np.arange(44100)/44100
                summed = np.zeros((len(t), 2), dtype=np.float64)
                for i, sid in enumerate(NATIVE_IDS[:count]):
                    tone = np.column_stack((np.sin(2*np.pi*(220+100*i)*t),)*2).astype('float32')*.01
                    sf.write(stems/(sid+'.wav'), tone, 44100, subtype='FLOAT')
                    summed += tone
                original = root/'original.wav'
                sf.write(original, summed, 44100, subtype='FLOAT')
                analysis = mix.analyze_project(original, stems, FFMPEG)
                self.assertEqual([s['id'] for s in analysis['stems']], list(NATIVE_IDS[:count]))
                self.assertLess(analysis['reconstructionResidual']['samplePeak'], 2e-6)
                plan = mix.build_plan(analysis, 'preserve')
                self.assertEqual(len(plan['stems']), count)
                invalid = copy.deepcopy(plan)
                invalid['stems'].pop()
                with self.assertRaises(ValueError):
                    mix.validate_plan(invalid, analysis)
                out = root/'out'
                result = mix.process_mix(original, stems, plan, out, FFMPEG, analysis=analysis)
                self.assertIsNone(result['residualRouting'])
                self.assertFalse((out/'processed_stems/other.wav').exists())
                rendered = sum(sf.read(out/'processed_stems'/(sid+'.wav'))[0] for sid in NATIVE_IDS[:count])
                np.testing.assert_allclose(sf.read(out/'candidate.wav')[0], rendered, atol=1e-7)
                np.testing.assert_allclose(sf.read(original)[0], rendered, atol=1e-7)
                # A fullmix-only component must fail rather than being assigned to a native track.
                sf.write(original, summed+.001, 44100, subtype='FLOAT')
                with self.assertRaisesRegex(ValueError, '重建残差'):
                    mix.analyze_project(original, stems, FFMPEG)
                with self.assertRaisesRegex(ValueError, '重建残差'):
                    mix.process_mix(original, stems, plan, root/'rejected', FFMPEG)
                self.assertFalse((root/'rejected').exists())


if __name__ == '__main__':
    unittest.main()

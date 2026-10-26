"""Verify an exported 7.1.4 studio ZIP without hardware or network claims."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import zipfile

import numpy as np
import soundfile as sf

ORDER = ['L', 'R', 'C', 'LFE', 'Ls', 'Rs', 'Lrs', 'Rrs', 'Ltf', 'Rtf', 'Ltr', 'Rtr']


def verify(path):
    with zipfile.ZipFile(path) as bundle:
        names = bundle.namelist()
        assert len(names) == len(set(names)), 'Duplicate ZIP entries'
        assert all(not n.startswith('/') and '..' not in Path(n).parts for n in names)
        report = json.loads(bundle.read('studio.json'))
        assert report['speakerLayout'] == '7.1.4' and report['channelOrder'] == ORDER
        assert report['physicalAcceptance'] == 'pending'
        assert report['perChannelNormalization'] is False
        assert len(report['channels']) == len(report['routingTest']) == 12
        assert len([n for n in names if n.startswith('surround_channels/')]) == 12
        assert len([n for n in names if n.startswith('routing_test/')]) == 12
        assert not any(n.startswith(('stems/', 'objects/')) or n == 'binaural.wav' for n in names)
        for item in report['channels'] + report['routingTest'] + report['documents']:
            assert hashlib.sha256(bundle.read(item['file'])).hexdigest() == item['sha256'], item['file']
        scene = json.loads(bundle.read('scene.json'))
        scene_hash = hashlib.sha256(json.dumps(scene, sort_keys=True, ensure_ascii=False,
                                              separators=(',', ':'), allow_nan=False).encode()).hexdigest()
        assert scene_hash == report['sceneHash'] and scene['speakerLayout'] == '7.1.4'
        expected_frames = round(scene['durationSeconds']*48000) + round(scene.get('renderTailSeconds', 0)*48000)
        levels = []
        for i, item in enumerate(report['channels']):
            assert item['channel'] == ORDER[i] and item['file'] == f'surround_channels/{i+1:02d}_{ORDER[i]}.wav'
            with sf.SoundFile(io.BytesIO(bundle.read(item['file']))) as wav:
                assert (wav.samplerate, wav.channels, len(wav), wav.subtype) == (48000, 1, expected_frames, 'PCM_24')
                audio = wav.read()
            assert np.isfinite(audio).all()
            peak, rms = float(np.max(np.abs(audio))), float(np.sqrt(np.mean(audio**2)))
            assert peak <= .950001 and abs(peak-item['peak']) < 1e-12 and abs(rms-item['rms']) < 1e-12
            if i == 3:
                assert peak == 0
            levels.append({'channel': ORDER[i], 'peak': peak, 'rms': rms, 'silent': peak == 0})
        occupied = np.zeros(24*48000, dtype=np.int8)
        for i, item in enumerate(report['routingTest']):
            assert item['channel'] == ORDER[i] and item['file'] == f'routing_test/{i+1:02d}_{ORDER[i]}.wav'
            with sf.SoundFile(io.BytesIO(bundle.read(item['file']))) as wav:
                assert (wav.samplerate, wav.channels, len(wav), wav.subtype) == (48000, 1, 24*48000, 'PCM_24')
                audio = wav.read()
            start, end = round((i*2+.5)*48000), round((i*2+1)*48000)
            assert item['startSeconds'] == start/48000 and item['endSeconds'] == end/48000
            assert not np.any(audio[:start]) and not np.any(audio[end:])
            occupied += (audio != 0)
            frequency, level = (50, -40) if i == 3 else (500, -30)
            assert abs(20*np.log10(np.max(np.abs(audio)))-level) < .001
            steady = audio[start+4800:end-4800]
            assert np.argmax(np.abs(np.fft.rfft(steady)))*48000/len(steady) == frequency
            assert abs(20*np.log10(np.sqrt(np.mean(steady**2)))-(level-10*np.log10(2))) < .001
        assert occupied.max() == 1, 'Simultaneous routing test channels'
    return {'package': str(path), 'passed': True, 'speakerLayout': '7.1.4', 'monoFiles': 12,
            'sampleRate': 48000, 'subtype': 'PCM_24', 'frames': expected_frames,
            'durationSeconds': expected_frames/48000, 'sceneHash': scene_hash,
            'hashesVerified': True, 'routingTestOneDigitalChannelAtATime': True,
            'programLfeSilent': True, 'physicalAcceptance': 'pending', 'levels': levels}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('package', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    result = verify(args.package)
    data = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)
    if args.output:
        args.output.write_text(data, encoding='utf-8')
    print(data)

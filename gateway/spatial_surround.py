"""Deterministic discrete speaker feeds; no Atmos bitstream."""
from functools import lru_cache
import json
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.spatial import ConvexHull

CHANNEL_ORDER = ("L", "R", "C", "LFE", "Ls", "Rs", "Tml", "Tmr")
STEREO_GEOMETRY = "great-circle-width-v1"
# A documented nominal geometry, not a measured or certified room.
SPEAKERS = ((30, 0), (-30, 0), (0, 0), None, (110, 0), (-110, 0), (90, 80), (-90, 80))
ACTIVE = np.array([0, 1, 2, 4, 5, 6, 7])
LAYOUTS = {
    "5.1.2": (CHANNEL_ORDER, SPEAKERS),
    "7.1.4": (("L", "R", "C", "LFE", "Ls", "Rs", "Lrs", "Rrs", "Ltf", "Rtf", "Ltr", "Rtr"),
              ((30, 0), (-30, 0), (0, 0), None, (100, 0), (-100, 0), (145, 0), (-145, 0),
               (45, 45), (-45, 45), (135, 45), (-135, 45))),
}


def layout_config(layout="5.1.2"):
    if not isinstance(layout, str) or layout not in LAYOUTS:
        raise ValueError("扬声器布局须为5.1.2或7.1.4。")
    return LAYOUTS[layout]


def surround_filename(layout="5.1.2"):
    layout_config(layout)
    return "surround_" + layout.replace(".", "_") + ".wav"


def directions(az, el):
    return np.column_stack((np.cos(az) * np.cos(el), np.sin(az) * np.cos(el), np.sin(el)))


@lru_cache(maxsize=2)
def triplets(layout):
    _, speakers = layout_config(layout)
    active = np.array([i for i, pos in enumerate(speakers) if pos is not None])
    vectors = directions(*np.radians(np.array([speakers[i] for i in active])).T)
    result = []
    for face in sorted(tuple(sorted(face)) for face in ConvexHull(vectors).simplices):
        basis = vectors[list(face)].T
        # Horizontal hull faces have zero volume and are not VBAP triplets.
        if abs(np.linalg.det(basis)) > 1e-8:
            result.append((active[list(face)], np.linalg.inv(basis)))
    return result


def speaker_gains(az, el, layout="5.1.2"):
    """Unit-energy VBAP; lower hemisphere projects continuously to the horizon."""
    vectors = directions(az, np.maximum(el, 0))
    gains = np.zeros((len(vectors), len(layout_config(layout)[0])), dtype=np.float64)
    assigned = np.zeros(len(vectors), dtype=bool)
    for channels, inverse in triplets(layout):
        candidate = vectors @ inverse.T
        valid = (~assigned) & (candidate.min(axis=1) >= -1e-9)
        gains[np.ix_(valid, channels)] = np.maximum(candidate[valid], 0)
        assigned |= valid
    if not assigned.all():
        raise ValueError("扬声器几何未覆盖声源方向；未生成无效输出。")
    gains /= np.linalg.norm(gains, axis=1)[:, None]
    return gains.astype(np.float32)


def write_surround(output_dir, mixed, rate, scene_hash, headroom, timeline, audio_stats, layout="5.1.2"):
    output_dir = Path(output_dir)
    order, speakers = layout_config(layout)
    subtype = "PCM_24" if layout == "7.1.4" else "FLOAT"
    sf.write(output_dir / surround_filename(layout), mixed, rate, format="WAV", subtype=subtype)
    stems = output_dir / "surround_channels"
    stems.mkdir()
    for index, name in enumerate(order):
        sf.write(stems / f"{index + 1:02d}_{name}.wav", mixed[:, index], rate, subtype=subtype)
    report = {"schema": "spatial-surround-v1", "sceneHash": scene_hash,
              "stereoGeometry": STEREO_GEOMETRY,
              "format": layout + " discrete speaker feeds", "speakerLayout": layout,
              "sampleRate": rate, "subtype": subtype,
              "channelOrder": order, "audio": audio_stats,
              "audioStatsDomain": "float render before file quantization",
              "channelMapping": "Project interchange order, not a DAW or hardware I/O standard. WAV has no speaker mask. "
                                "surround_channels/ mono files are the unambiguous interchange.",
              "speakers": [{"channel": name, "file": f"surround_channels/{i + 1:02d}_{name}.wav",
                            "azimuthDeg": speakers[i][0] if speakers[i] else None,
                            "elevationDeg": speakers[i][1] if speakers[i] else None}
                           for i, name in enumerate(order)],
              "coordinates": "0 front, +90 left, -90 right, +/-180 rear; elevation +90 up",
              "panning": "Convex-hull VBAP, nonnegative triplet gains normalized to unit squared sum; "
                         "stereo sources at great-circle offsets +/- width/2 along the center's horizontal tangent, "
                         "each gain / sqrt(2). "
                         "Shortest-arc trajectories; negative elevation clamped continuously to 0.",
              "lfe": "Silent; no bass routing, bass management, crossover or +10 dB calibration applied.",
              "mixHeadroomGain": headroom, "headroomPolicy": "One common attenuation for FOA and all speaker feeds; no per-channel normalization.",
              "timeline": timeline,
              "references": ["https://www.dolby.com/siteassets/about/support/guide/setup-guides/7.1.4-overhead-speaker-placement/7_1_4_overhead_speaker_setup.pdf",
                             "https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/ksmedia/ns-ksmedia-waveformatextensible"],
              "limitation": "Nominal speaker geometry only; lower hemisphere projects to horizon. "
                            "No Dolby Atmos ADM, encoder, room calibration or physical listening certification."}
    (output_dir / "surround.json").write_text(json.dumps(report, ensure_ascii=False, indent=2,
                                                       allow_nan=False), encoding="utf-8")
    return report

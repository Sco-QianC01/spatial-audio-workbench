"""Signal relationships for guided listening; no learned or perceptual quality score."""
from itertools import combinations

import numpy as np
from scipy.signal import spectrogram

BANDS = (20, 60, 120, 250, 500, 1000, 2000, 4000, 8000, 20000)
WINDOW_SECONDS = .5


def stereo_windows(data, rate):
    windows = []
    size = round(rate * WINDOW_SECONDS)
    for start in range(0, len(data), size):
        x = data[start:start+size].astype(np.float64)
        power = float(np.mean(x*x))
        mono_power = float(np.mean(np.mean(x, axis=1)**2))
        centered = x - x.mean(axis=0)
        energies = np.sum(centered*centered, axis=0)
        denom = float(np.sqrt(energies.prod()))
        correlation = float(np.clip(np.sum(centered[:, 0]*centered[:, 1])/denom, -1, 1)) \
            if power > 1e-6 and denom > 1e-18 else None
        loss = 10 * np.log10(mono_power/power) if power > 1e-12 and mono_power > 1e-20 else None
        cancelled = bool(power > 1e-6 and mono_power <= 1e-20)
        risk = bool(cancelled or (correlation is not None and correlation < -.25
                                 and loss is not None and loss < -6))
        windows.append({"startSeconds": start/rate, "endSeconds": (start+len(x))/rate,
                        "correlation": correlation, "monoCancellationDb": loss,
                        "monoFullyCancelled": cancelled, "monoRisk": risk})
    return {"windowSeconds": WINDOW_SECONDS, "activeThresholdDbFS": -60,
            "method": "zero-lag AC correlation; mono=(L+R)/2 at unchanged gain",
            "riskRule": "active window; full cancellation or correlation < -0.25 and mono loss < -6dB; project heuristic",
            "riskDurationSeconds": sum(v["endSeconds"]-v["startSeconds"] for v in windows if v["monoRisk"]),
            "windows": windows}


def band_profile(data, rate):
    n = min(2048, len(data))
    frequencies, times, power = spectrogram(data, fs=rate, nperseg=n, noverlap=0,
                                           window="hann", axis=0, detrend=False,
                                           scaling="density", mode="psd")
    power = power.mean(axis=1, dtype=np.float64)
    count = max(1, int(np.ceil(len(data)/rate/WINDOW_SECONDS)))
    indexes = np.minimum((times/WINDOW_SECONDS).astype(int), count-1)
    occupancy = np.bincount(indexes, minlength=count)
    bands = np.zeros((len(BANDS)-1, count))
    for i, (lo, hi) in enumerate(zip(BANDS, BANDS[1:])):
        energy = power[(frequencies >= lo) & (frequencies < min(hi, rate/2+1))].sum(axis=0)
        energy *= rate/n
        sums = np.bincount(indexes, weights=energy, minlength=count)
        bands[i] = np.divide(sums, occupancy, out=np.zeros(count), where=occupancy > 0)
    return bands


def analyze_relationships(stems, rate):
    items = stems.items() if hasattr(stems, "items") else stems
    profiles = {sid: band_profile(data, rate) for sid, data in items}
    pairs = []
    for a, b in combinations(profiles, 2):
        x, y = profiles[a], profiles[b]
        total_x, total_y = x.sum(axis=0), y.sum(axis=0)
        active = (total_x > 1e-6) & (total_y > 1e-6)
        level_difference = np.abs(10*np.log10(np.maximum(x, 1e-18)/np.maximum(y, 1e-18)))
        relevant = (x > 1e-6) & (y > 1e-6) & (level_difference <= 8)
        relevant &= (x >= total_x*.05) & (y >= total_y*.05) & active
        simultaneous = relevant.any(axis=0)
        fraction = float(simultaneous.mean())
        if fraction < .25:
            continue
        bands = [f"{lo}–{hi} Hz" for i, (lo, hi) in enumerate(zip(BANDS, BANDS[1:]))
                 if relevant[i].any()]
        at = np.flatnonzero(simultaneous)
        pairs.append({"ids": [a, b], "bands": bands, "overlapFraction": fraction,
                      "firstSeconds": float(at[0]*WINDOW_SECONDS),
                      "meaning": "同频段同时出现且电平接近，供相互试听；不表示冲突或必须EQ"})
    return {"schema": "spatial-relationships-v1", "windowSeconds": WINDOW_SECONDS,
            "method": "Hann spectrum averaged in 0.5s windows; band/time/level overlap, not a psychoacoustic masking or quality model",
            "rules": {"activeBandDbFS": -60, "maxLevelDifferenceDb": 8,
                      "minBandPowerFraction": .05, "minOverlapFraction": .25},
            "overlapPairs": pairs}

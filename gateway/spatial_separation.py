"""Offline stereo HDemucs separation with bounded overlap-add buffers."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

import numpy as np
import soundfile as sf
import torch

SOURCES = ("drums", "bass", "other", "vocals")
SAMPLE_RATE = 44100
CHUNK_FRAMES = 6 * SAMPLE_RATE
OVERLAP_FRAMES = SAMPLE_RATE * 3 // 2


def progress(**values):
    print(json.dumps(values, allow_nan=False), flush=True)


def audio_stats(audio):
    return {"finite": bool(np.isfinite(audio).all()),
            "rms": float(np.sqrt(np.mean(np.square(audio, dtype=np.float64)))),
            "peak": float(np.max(np.abs(audio)))}


def inspect_input(path):
    with sf.SoundFile(path) as stream:
        if stream.format not in ("WAV", "WAVEX") or stream.samplerate != SAMPLE_RATE or stream.channels != 2:
            raise ValueError("Input must be a 44100 Hz stereo WAV")
        if not stream.frames:
            raise ValueError("Input WAV is empty")
        square_sum, peak = 0.0, 0.0
        for block in stream.blocks(blocksize=SAMPLE_RATE, dtype="float32", always_2d=True):
            stats = audio_stats(block)
            if not stats["finite"]:
                raise ValueError("Input WAV contains nonfinite samples")
            square_sum += float(np.square(block, dtype=np.float64).sum())
            peak = max(peak, stats["peak"])
        return {"frames": stream.frames, "channels": 2, "sampleRate": SAMPLE_RATE,
                "shape": [stream.frames, 2], "finite": True,
                "rms": float(np.sqrt(square_sum / (stream.frames * 2))), "peak": peak}


def load_model(weights):
    from torchaudio.models import hdemucs_high

    digest = hashlib.sha256()
    with weights.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    state = torch.load(weights, map_location="cpu", weights_only=True, mmap=True)
    if not isinstance(state, dict) or not state or not all(
            isinstance(value, torch.Tensor) for value in state.values()):
        raise ValueError("Weights must be a tensor-only HDemucs state dictionary")
    model = hdemucs_high(sources=list(SOURCES))
    model.load_state_dict(state, strict=True)
    model.eval()
    return model, {"name": "torchaudio HDemucs high", "strictLoad": True,
                   "tensorCount": len(state), "weightSHA256": digest.hexdigest(),
                   "device": "cpu", "torchVersion": torch.__version__}


def separated_blocks(model, read_chunk, frames, chunk_frames=CHUNK_FRAMES,
                     overlap_frames=OVERLAP_FRAMES, on_progress=None):
    """Yield final samples once; retain only one overlap-add window in memory."""
    if frames <= 0 or chunk_frames < 2 or not 0 <= overlap_frames < chunk_frames:
        raise ValueError("Invalid frame count or overlap")
    step = chunk_frames - overlap_frames
    weight = torch.ones(chunk_frames, dtype=torch.float32)
    if overlap_frames:
        ramp = torch.arange(1, overlap_frames + 1, dtype=torch.float32) / (overlap_frames + 1)
        weight[:overlap_frames] = torch.minimum(weight[:overlap_frames], ramp)
        weight[-overlap_frames:] = torch.minimum(weight[-overlap_frames:], ramp.flip(0))
    total = torch.zeros((4, 2, chunk_frames), dtype=torch.float32)
    denominator = torch.zeros(chunk_frames, dtype=torch.float32)
    with torch.inference_mode():
        for index, start in enumerate(range(0, frames, step)):
            length = min(chunk_frames, frames - start)
            audio = np.asarray(read_chunk(start, length), dtype=np.float32)
            if audio.shape != (length, 2) or not np.isfinite(audio).all():
                raise ValueError("Input reader returned invalid stereo samples")
            batch = torch.from_numpy(audio.T.copy()).unsqueeze(0)
            batch = torch.nn.functional.pad(batch, (0, chunk_frames - length))
            predicted = model(batch)
            if tuple(predicted.shape) != (1, 4, 2, chunk_frames) or not torch.isfinite(predicted).all():
                raise ValueError("Model returned invalid or nonfinite stems")
            total[:, :, :length] += predicted[0, :, :, :length] * weight[:length]
            denominator[:length] += weight[:length]
            count = min(step, frames - start)
            if not torch.all(denominator[:count] > 0):
                raise RuntimeError("Uncovered samples in overlap-add")
            block = (total[:, :, :count] / denominator[:count]).numpy().copy()
            if not np.isfinite(block).all():
                raise ValueError("Overlap-add produced nonfinite samples")
            yield block
            # Clone the overlapping slices because source and destination share storage.
            total[:, :, :overlap_frames] = total[:, :, step:].clone()
            denominator[:overlap_frames] = denominator[step:].clone()
            total[:, :, overlap_frames:] = 0
            denominator[overlap_frames:] = 0
            if on_progress:
                on_progress(stage="separating", chunk=index + 1,
                            completedFrames=start + count, totalFrames=frames)


def separate(input_path, output_dir, weights):
    started = time.monotonic()
    input_path, output_dir, weights = (Path(value).resolve() for value in
                                       (input_path, output_dir, weights))
    if not input_path.is_file() or not weights.is_file():
        raise ValueError("Input WAV and local weights must exist")
    if output_dir.exists() or input_path == weights or input_path in output_dir.parents or weights in output_dir.parents:
        raise ValueError("Output directory must be new and separate from input and weights")
    input_info = inspect_input(input_path)
    torch.set_num_threads(4)
    progress(stage="loading", totalFrames=input_info["frames"])
    model, model_info = load_model(weights)
    output_dir.mkdir(parents=True, exist_ok=False)
    square_sum = np.zeros(4, dtype=np.float64)
    peaks = np.zeros(4, dtype=np.float64)
    residual_square, residual_peak = 0.0, 0.0
    written = 0
    from contextlib import ExitStack

    with ExitStack() as stack:
        reader = stack.enter_context(sf.SoundFile(input_path))
        writers = [stack.enter_context(sf.SoundFile(output_dir / f"{source}.wav", mode="x",
                   samplerate=SAMPLE_RATE, channels=2, subtype="FLOAT", format="WAV"))
                   for source in SOURCES]

        def read_chunk(start, length):
            reader.seek(start)
            return reader.read(length, dtype="float32", always_2d=True)

        for block in separated_blocks(model, read_chunk, input_info["frames"], on_progress=progress):
            residual = read_chunk(written, block.shape[-1]).astype(np.float64) - block.sum(axis=0, dtype=np.float64).T
            residual_square += float(np.square(residual).sum())
            residual_peak = max(residual_peak, float(np.max(np.abs(residual))))
            for index, writer in enumerate(writers):
                writer.write(block[index].T)
                square_sum[index] += np.square(block[index], dtype=np.float64).sum()
                peaks[index] = max(peaks[index], float(np.max(np.abs(block[index]))))
            written += block.shape[-1]
    if written != input_info["frames"]:
        raise RuntimeError("Output length differs from input")
    outputs = {}
    for index, source in enumerate(SOURCES):
        path = output_dir / f"{source}.wav"
        info = sf.info(path)
        if info.frames != written or info.channels != 2 or info.subtype != "FLOAT":
            raise RuntimeError("Written WAV metadata differs from expected output")
        outputs[source] = {"file": path.name, "frames": written, "channels": 2,
                           "sampleRate": SAMPLE_RATE, "shape": [written, 2], "subtype": "FLOAT",
                           "finite": True, "rms": float(np.sqrt(square_sum[index] / (written * 2))),
                           "peak": float(peaks[index])}
    residual_rms = float(np.sqrt(residual_square / (written * 2)))
    report = {"model": model_info, "input": input_info, "outputs": outputs,
              "sources": list(SOURCES), "normalization": "none",
              "reconstruction": {"residualRms": residual_rms, "residualPeak": residual_peak,
                  "relativeRms": residual_rms / input_info["rms"] if input_info["rms"] > 0 else None,
                  "meaning": "Original minus unity sum of estimated stems; not isolation or listening quality.",
                  "residualAddedToStems": False},
              "chunkFrames": CHUNK_FRAMES, "overlapFrames": OVERLAP_FRAMES,
              "cpuThreads": torch.get_num_threads(), "elapsedSeconds": time.monotonic() - started}
    with (output_dir / "separation.json").open("x", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, allow_nan=False)
    progress(stage="complete", report="separation.json", elapsedSeconds=report["elapsedSeconds"])
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_wav")
    parser.add_argument("output_dir")
    parser.add_argument("--weights", required=True)
    args = parser.parse_args()
    try:
        separate(args.input_wav, args.output_dir, args.weights)
    except Exception as error:
        progress(stage="error", error=str(error))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

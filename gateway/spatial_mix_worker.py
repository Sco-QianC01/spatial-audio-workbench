"""A bounded child process for local analysis/DSP; no network or model calls."""
import json
from pathlib import Path
import sys

from spatial_mix import analyze_project, process_mix


def main():
    job_path = Path(sys.argv[1]).resolve()
    job = json.loads(job_path.read_text(encoding="utf-8"))
    if job["mode"] == "analyze":
        report = analyze_project(Path(job["original"]), Path(job["stems"]),
                                 job["ffmpeg"],
                                 Path(job["reference"]) if job.get("reference") else None)
    elif job["mode"] == "process":
        report = process_mix(Path(job["original"]), Path(job["stems"]),
                             job["plan"], job_path.parent / "audio", job["ffmpeg"],
                             analysis=job["analysis"])
    else:
        raise ValueError("未知本地混音任务。")
    dest = job_path.parent / "result.json"
    temp = dest.with_suffix(".tmp")
    temp.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False),
                    encoding="utf-8")
    temp.replace(dest)


if __name__ == "__main__":
    main()

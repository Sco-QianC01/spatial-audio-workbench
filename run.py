"""Start the workbench using this checkout's private virtual environment."""
import argparse
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / ".runtime" / "config.json"
APP = "neural-spatial-audio"


def clean_environment():
    env = {key: value for key, value in os.environ.items()
           if key.upper() not in ("PYTHONPATH", "PYTHONHOME")}
    env["PYTHONNOUSERSITE"] = "1"
    return env


def require_environment():
    expected = ROOT / ".venv"
    cfg = expected / "pyvenv.cfg"
    values = {}
    if cfg.is_file():
        values = dict(line.lower().split("=", 1) for line in cfg.read_text().splitlines() if "=" in line)
        values = {key.strip(): value.strip() for key, value in values.items()}
    if Path(sys.prefix).resolve() != expected.resolve() or sys.prefix == sys.base_prefix \
            or values.get("include-system-site-packages") != "false":
        raise ValueError("Use this repository's .venv Python; create it without system-site-packages first.")


def local_path(value):
    return Path(value).expanduser().absolute().resolve()


def configuration(args):
    saved = {}
    if CONFIG.is_file():
        saved = json.loads(CONFIG.read_text(encoding="utf-8"))
        if not isinstance(saved, dict) or set(saved) - {"weights", "output", "downloads", "ffmpeg"}:
            raise ValueError("Invalid local path configuration.")
        if any(not isinstance(value, str) or not value for value in saved.values()):
            raise ValueError("Configuration values must be nonempty local paths.")
    values = {name: getattr(args, name) or saved.get(name) for name in ("weights", "output", "downloads", "ffmpeg")}
    if not values["weights"]:
        raise ValueError("Provide --weights with an existing local model file; no model is downloaded.")
    result = {name: local_path(value) for name, value in values.items() if value}
    result.setdefault("output", ROOT / ".runtime" / "projects")
    if not result["weights"].is_file():
        raise ValueError("The configured weights file does not exist.")
    for name in ("output", "downloads"):
        path = result.get(name)
        if path and ((path.exists() and not path.is_dir()) or
                     any(parent.exists() and not parent.is_dir() for parent in path.parents)):
            raise ValueError(f"{name} must be a directory path.")
    ffmpeg = result.get("ffmpeg") or shutil.which("ffmpeg")
    if not ffmpeg or not Path(ffmpeg).is_file() or not os.access(ffmpeg, os.X_OK):
        raise ValueError("Provide --ffmpeg with an executable, or install FFmpeg on PATH.")
    result["ffmpeg"] = local_path(ffmpeg)
    return result


def status(port):
    # Ignore system proxy settings for this loopback-only readiness probe.
    opener = build_opener(ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{port}/api/spatial/status", timeout=1) as response:
            data = json.loads(response.read(1024 * 1024))
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError, URLError):
        return {}


def check_instance(data):
    if data.get("app") != APP:
        raise ValueError("Port is occupied by another application; choose another --port.")
    environment = data.get("environment", {})
    if environment.get("sourceRoot") != str(ROOT):
        raise ValueError("Port belongs to an old or different checkout; stop it yourself or choose another --port.")
    return data.get("ready") is True and environment.get("isolated") is True \
        and data.get("separation", {}).get("ready") is True \
        and data.get("mixing", {}).get("ready") is True


def port_occupied(port):
    with socket.socket() as probe:
        return probe.connect_ex(("127.0.0.1", port)) == 0


def background(args, paths):
    if port_occupied(args.port):
        if check_instance(status(args.port)):
            print(f"Ready: http://127.0.0.1:{args.port}/spatial-audio/")
            return
        raise ValueError("This checkout is running but is not ready; inspect its existing log.")
    runtime = ROOT / ".runtime"
    runtime.mkdir(exist_ok=True)
    command = [sys.executable, "-I", str(ROOT / "run.py"), "--port", str(args.port)]
    for name, value in paths.items():
        command.extend(["--" + name, str(value)])
    options = {"start_new_session": True} if os.name != "nt" else {
        "creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
    with (runtime / "server.log").open("ab") as log:
        child = subprocess.Popen(command, cwd=ROOT, env=clean_environment(), stdin=subprocess.DEVNULL,
                                 stdout=log, stderr=subprocess.STDOUT, **options)
    (runtime / "server.json").write_text(json.dumps({
        "pid": child.pid, "port": args.port, "root": str(ROOT)}, indent=2) + "\n", encoding="utf-8")
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        if child.poll() is not None:
            raise ValueError("Server exited; inspect .runtime/server.log.")
        data = status(args.port)
        if data and check_instance(data):
            print(f"Ready: http://127.0.0.1:{args.port}/spatial-audio/")
            return
        time.sleep(0.25)
    raise ValueError("Server was not ready within 45 seconds; inspect .runtime/server.log. No process was stopped.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("weights", "output", "downloads", "ffmpeg"):
        parser.add_argument("--" + name, help="Local path" + (" (default: .runtime/projects)" if name == "output" else ""))
    parser.add_argument("--port", type=int, default=8773)
    parser.add_argument("--configure", action="store_true", help="Save local paths without starting the server")
    parser.add_argument("--background", action="store_true", help="Start detached and wait for readiness")
    args = parser.parse_args(argv)
    try:
        require_environment()
        if not 1 <= args.port <= 65535:
            raise ValueError("--port must be between 1 and 65535.")
        paths = configuration(args)
        if args.configure:
            CONFIG.parent.mkdir(exist_ok=True)
            temporary = CONFIG.with_suffix(".tmp")
            temporary.write_text(json.dumps({key: str(value) for key, value in paths.items()}, indent=2) + "\n", encoding="utf-8")
            temporary.replace(CONFIG)
            print("Saved local paths to .runtime/config.json")
            return 0
        if args.background:
            background(args, paths)
            return 0
        environment = clean_environment()
        os.environ.clear()
        os.environ.update(environment)
        # Gateway modules already use sibling imports; keep that existing convention.
        sys.path.insert(0, str(ROOT / "gateway"))
        from aiohttp import web
        from spatial_runtime import create_app
        web.run_app(create_app(paths["output"], sys.executable, paths["weights"],
                               downloads=paths.get("downloads"), ffmpeg=str(paths["ffmpeg"])),
                    host="127.0.0.1", port=args.port)
        return 0
    except ImportError as exc:
        parser.error(f"The repository .venv is missing a dependency: {exc}. Install requirements.txt into .venv.")
    except (ValueError, OSError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    clean_environment_snapshot = clean_environment()
    if not sys.flags.isolated:
        os.execve(sys.executable, [sys.executable, "-I", str(ROOT / "run.py"), *sys.argv[1:]],
                  clean_environment_snapshot)
    raise SystemExit(main())

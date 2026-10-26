"""Run only this independent workbench's spatial test modules."""
import argparse
from pathlib import Path
import sys
import unittest


def is_spatial(name):
    return name.startswith("test_spatial_") or name == "test_ace_native_producer.py"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=("spatial",), default="spatial")
    parser.add_argument("--check-selection", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    tests = root / "gateway" / "tests"
    paths = sorted(tests.glob("test_*.py"))
    selected = [path for path in paths if is_spatial(path.name) == (args.suite == "spatial")]
    if args.check_selection:
        assert is_spatial("test_spatial_audio.py")
        assert is_spatial("test_ace_native_producer.py")
        assert not is_spatial("test_spatially_named_core.py")
        assert not is_spatial("test_runtime.py")
        core = {path for path in paths if not is_spatial(path.name)}
        spatial = {path for path in paths if is_spatial(path.name)}
        assert not core and spatial
        assert core | spatial == set(paths)
        print(f"Selection verified: {len(spatial)} spatial modules; no foreign test modules or imports.")
        return 0
    if not selected:
        parser.error("No test modules selected.")
    sys.path.insert(0, str(root))
    sys.path.insert(0, str(root / "gateway"))
    loader = unittest.TestLoader()
    suite = unittest.TestSuite(loader.discover(str(tests), pattern=path.name) for path in selected)
    return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())

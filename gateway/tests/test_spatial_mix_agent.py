"""Coordinator boundaries, honest technical states and portable audition output."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("mix_agent", Path(__file__).resolve().parents[2] /
                                             "tools/spatial-mix-agent.py")
agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent)


class MixAgentTests(unittest.TestCase):
    def test_spatial_candidate_requires_preview_before_any_tools(self):
        for layout in ("content", "spacious"):
            with self.subTest(layout=layout), patch.object(
                    agent.sys, "argv", ["spatial-mix-agent.py", "--project", "a"*32,
                                       "--output", "unused", "--spatial-layout", layout]), \
                    patch.object(agent, "LocalTools") as tools, \
                    patch.object(agent.sys, "stderr"):
                self.assertEqual(agent.main(), 1)
                tools.assert_not_called()

    def test_import_route_is_explicit_and_never_falls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)/'stereo.wav'
            source.write_bytes(b'test boundary only, server owns audio decoding')
            tools = agent.LocalTools('http://127.0.0.1:8773')
            with patch.object(tools, 'json', return_value={'id': 'a'*32}) as call:
                with self.assertRaises(ValueError):
                    tools.import_file(source)
                call.assert_not_called()
                value = tools.import_file(source, 'separate')
                self.assertEqual(call.call_args.args[0], '/api/spatial/import')
                self.assertIn(b'filename="stereo-source.audio"', call.call_args.args[1])
                self.assertEqual(value['importedFile']['bytes'], source.stat().st_size)

    def test_only_explicit_loopback_tools_without_redirects(self):
        for value in ("https://127.0.0.1:8773", "http://example.com:8773", "http://127.0.0.1",
                      "http://u:p@127.0.0.1:8773", "http://localhost:8773/path", "http://localhost:8773?x=y"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                agent.LocalTools(value)
        tools = agent.LocalTools("http://127.0.0.1:8773")
        for path in ("https://example.com", "/api/spatial/../private", "/api/spatial/status?token=a"):
            with self.assertRaises(ValueError):
                tools.request(path)
        with self.assertRaises(ValueError):
            agent.NoRedirect().redirect_request(None, None, None, None, None, None)

    def test_focus_rejects_nonfinite_reverse_or_outside_source(self):
        for item in (("nan", "2", "note"), ("0", "inf", "note"), ("2", "1", "note"),
                     ("0", "3.1", "note"), ("0", "1", "")):
            with self.subTest(item=item), self.assertRaises(ValueError):
                agent.validate_focus([item], 3)
        self.assertEqual(agent.validate_focus([(".5", "2", "主体")], 3)[0]["startSeconds"], .5)
        with self.assertRaises(ValueError):
            agent.validate_focus([("0", "1", "note")] * 17, 3)

    def test_good_measurements_never_approve_listening(self):
        report = {"hashes": {"original": "source"}, "loudnessMatch": {"status": "matched"},
                  "metrics": {"candidate": {"truePeakDbTP": -1.1},
                              "baselineMatched": {"integratedLufs": -20},
                              "candidateMatched": {"integratedLufs": -20.1}}}
        checks = agent.technical_checks(report, "source")
        self.assertTrue(checks["loudnessMatched"])
        self.assertFalse(checks["humanListeningVerified"])
        self.assertFalse(checks["physicalSpeakerSystemVerified"])
        report["metrics"]["candidate"]["truePeakDbTP"] = float("nan")
        report["metrics"]["candidateMatched"]["integratedLufs"] = None
        checks = agent.technical_checks(report, "changed")
        self.assertFalse(checks["sourceHashUnchanged"])
        self.assertFalse(checks["candidateTruePeakWithinProjectLimit"])
        self.assertFalse(checks["loudnessMatched"])

    def test_output_is_new_and_audition_escapes_local_notes(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "run"
            runner = agent.Coordinator(None, folder, 30)
            with self.assertRaises(FileExistsError):
                agent.Coordinator(None, folder, 30)
            runner.record.update(focus=agent.validate_focus([("0", "1", "<script>wrong</script>")], 3),
                                 technicalChecks={"loudnessMatched": False}, listeningTasks=["检查中文与<符号"])
            agent.write_audition(folder, runner.record)
            text = (folder / "试听.html").read_text(encoding="utf-8")
            self.assertNotIn("<script>wrong</script>", text)
            self.assertIn("&lt;script&gt;", text)
            self.assertIn("匹配不可用", text)
            self.assertNotIn("A/B为0.2 LU以内", text)
            runner.event("awaiting-listening")
            self.assertIsNone(json.loads((folder / "run.json").read_text())["selectedMix"])


if __name__ == "__main__":
    unittest.main()

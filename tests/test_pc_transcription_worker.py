"""pc_client transcription worker and runner with Demucs/Whisper mocked."""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from pc_client import transcribe_runner as runner
from pc_client import transcription_worker as tw

GPU = {"gpu": True, "gpu_name": "RTX 4070", "vram_mb": 12282, "gpu_percent": 4.0}
NO_GPU = {"gpu": False, "gpu_name": "", "vram_mb": 0, "gpu_percent": None}


class FakeProcess:
    def __init__(self, stdout=(), stderr=(), returncode=0, polls_before_exit=0, on_start=None):
        self.stdout = list(stdout)
        self.stderr = list(stderr)
        self.returncode = None
        self._rc = returncode
        self._polls = polls_before_exit
        self.pid = -1
        self.killed = False
        if on_start:
            on_start()

    def poll(self):
        if self.killed:
            self.returncode = -9
        elif self._polls <= 0:
            self.returncode = self._rc
        else:
            self._polls -= 1
        return self.returncode

    def wait(self):
        self.returncode = self._rc
        return self._rc

    def kill(self):
        self.killed = True


class ReadyRuntime(tw.Runtime):
    def __init__(self, root: Path):
        super().__init__(root, root)
        self.env_ready = True

    def ready(self):
        return self.env_ready


class HardwareTests(unittest.TestCase):
    def test_gpu_info_picks_largest_nvidia_gpu_and_handles_absence(self):
        output = "NVIDIA GeForce GTX 1650, 4096, 12\nNVIDIA GeForce RTX 4070, 12282, 3\n"
        info = tw.gpu_info(run=lambda _args: output)
        self.assertEqual((info["gpu"], info["gpu_name"], info["vram_mb"], info["gpu_percent"]),
                         (True, "NVIDIA GeForce RTX 4070", 12282, 3.0))
        self.assertFalse(tw.gpu_info(run=lambda _args: "")["gpu"])
        self.assertFalse(tw.gpu_info(run=lambda _args: "garbage")["gpu"])

    def test_capabilities_and_busy_threshold(self):
        caps = tw.capabilities(GPU)
        self.assertEqual((caps["gpu"], caps["vram_mb"]), (True, 12282))
        self.assertGreaterEqual(caps["cpu_cores"], 1)
        self.assertGreater(caps["ram_mb"], 0)
        with patch.object(tw.psutil, "cpu_percent", return_value=40.0):
            self.assertFalse(tw.current_load(GPU, interval=0)["busy"])
            self.assertTrue(tw.current_load({**GPU, "gpu_percent": 71}, interval=0)["busy"])
            self.assertTrue(tw.current_load(GPU, running=True, interval=0)["busy"])
        with patch.object(tw.psutil, "cpu_percent", return_value=70.5):
            self.assertTrue(tw.current_load(NO_GPU, interval=0)["busy"])
        self.assertEqual(tw.thread_limit({"cpu_cores": 16}), 4)
        self.assertEqual(tw.thread_limit({"cpu_cores": 6}), 3)
        self.assertEqual(tw.thread_limit({"cpu_cores": 1}), 1)


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_install_plan_uses_cuda_wheels_only_with_gpu_and_warms_models(self):
        runtime = tw.Runtime(self.root, self.root)
        gpu = runtime.install_commands(["py", "-3.11"], gpu=True)
        cpu = runtime.install_commands(["py", "-3.11"], gpu=False)
        self.assertEqual(gpu[0], ["py", "-3.11", "-m", "venv", str(runtime.env)])
        self.assertIn(tw.TORCH_CUDA_INDEX, gpu[2])
        self.assertIn(tw.TORCH_CPU_INDEX, cpu[2])
        self.assertTrue(any("faster-whisper" in item for item in gpu[3]))
        self.assertTrue(any("demucs" in item for item in gpu[3]))
        self.assertIn("--warmup", gpu[4])
        self.assertIn("cuda", gpu[4])
        self.assertIn("cpu", cpu[4])
        # Models live in the runtime cache, never in the repository.
        self.assertTrue(str(runtime.models).startswith(str(self.root)))

    def test_install_runs_steps_at_low_priority_and_writes_marker(self):
        runtime = tw.Runtime(self.root, self.root)
        calls = []
        with patch.object(runtime, "find_base_python", return_value=["python3.11"]):
            runtime.install(False, popen=lambda cmd, **kw: calls.append((cmd, kw)) or FakeProcess())
        self.assertEqual(len(calls), 5)
        self.assertTrue(runtime.marker.is_file())
        failing = tw.Runtime(self.root / "other", self.root)
        with patch.object(failing, "find_base_python", return_value=["python3.11"]):
            with self.assertRaises(tw.TranscriptionError) as ctx:
                failing.install(False, popen=lambda cmd, **kw: FakeProcess(returncode=1))
        self.assertEqual(ctx.exception.reason, "install_step_1_failed")
        with patch.object(failing, "find_base_python", return_value=None):
            with self.assertRaises(tw.TranscriptionError):
                failing.install(False)

    def test_find_base_python_accepts_only_supported_versions(self):
        runtime = tw.Runtime(self.root, self.root, {"transcription_python": "C:/Py/python.exe"})
        versions = {"C:/Py/python.exe": "3.13", "python3.12": "3.12"}
        found = runtime.find_base_python(run=lambda args: versions.get(args[0], ""))
        self.assertEqual(found, ["python3.12"])

    def test_low_priority_and_thread_limits(self):
        runtime = tw.Runtime(self.root, self.root)
        env = runtime.environment(3)
        self.assertEqual((env["OMP_NUM_THREADS"], env["MKL_NUM_THREADS"]), ("3", "3"))
        self.assertTrue(env["HF_HOME"].startswith(str(runtime.models)))
        idle_runtime = tw.Runtime(self.root, self.root, {"transcription_priority": "idle"})
        with patch.object(tw.os, "name", "nt"):
            flags = runtime.popen_kwargs()["creationflags"]
            self.assertTrue(flags & tw.BELOW_NORMAL_PRIORITY_CLASS)
            self.assertTrue(flags & tw.CREATE_NO_WINDOW)
            idle = idle_runtime.popen_kwargs()["creationflags"]
            self.assertTrue(idle & tw.IDLE_PRIORITY_CLASS)
        if os.name != "nt":
            self.assertIn("preexec_fn", runtime.popen_kwargs())


class WorkerFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.requests = []
        self.progress_status = 200
        self.job = {"id": 7, "track_id": 3, "title": "Трек", "duration": 180, "mime": "audio/mpeg", "filename": "t.mp3",
                    "language": "ru", "media_path": "/agent/music/tracks/3/stream?ticket=abc"}
        self.poll_job = self.job

    def tearDown(self):
        self.temp.cleanup()

    def handler(self, request: httpx.Request):
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.method, request.url.path, body, request.headers.get("x-api-key")))
        if request.url.path == "/agent/transcription/poll":
            job, self.poll_job = self.poll_job, None
            return httpx.Response(200, json={"ok": True, "job": job})
        if request.url.path.endswith("/stream"):
            return httpx.Response(200, content=b"ID3-audio-bytes")
        if request.url.path.endswith("/progress"):
            return httpx.Response(self.progress_status, json={"ok": True})
        return httpx.Response(200, json={"ok": True, "state": "done"})

    def worker(self, popen, *, enabled=True, gpu=GPU):
        config = {"server_url": "https://xass.example", "api_key": "ag_fixture", tw.CONFIG_KEY: enabled}
        runtime = ReadyRuntime(self.root)
        worker = tw.TranscriptionWorker(config, self.root, self.root, runtime=runtime, popen=popen,
            client_factory=lambda: httpx.Client(transport=httpx.MockTransport(self.handler)),
            probe_gpu=lambda: gpu, sleep=lambda _s: None)
        return worker

    def paths(self):
        return [(method, path) for method, path, _, _ in self.requests]

    def test_enabled_worker_reports_capabilities_downloads_runs_runner_and_posts_lines(self):
        launched = {}

        def popen(command, **kwargs):
            launched.update(command=command, kwargs=kwargs)
            source = Path(command[command.index("--input") + 1])
            launched["audio"] = source.read_bytes()
            output = Path(command[command.index("--output") + 1])
            output.write_text(json.dumps({"lines": [{"start": 1.0, "end": 2.5, "text": "первая"}, {"start": 3, "end": 4, "text": " "}],
                                          "language": "ru", "model": "large-v3", "device": "cuda"}), encoding="utf-8")
            return FakeProcess(stdout=["PROGRESS separate 0.500\n", "PROGRESS transcribe 0.900\n"], polls_before_exit=2)

        worker = self.worker(popen)
        with patch.object(tw.psutil, "cpu_percent", return_value=10.0):
            thread = threading.Thread(target=worker.run_forever)
            original_process = worker.process

            def process_once(client, job):
                result = original_process(client, job)
                worker.stop.set()
                return result
            worker.process = process_once
            thread.start(); thread.join(10)
        self.assertFalse(thread.is_alive())
        poll = self.requests[0]
        self.assertEqual((poll[1], poll[3]), ("/agent/transcription/poll", "ag_fixture"))
        self.assertEqual(poll[2]["capabilities"]["vram_mb"], 12282)
        self.assertTrue(poll[2]["enabled"])
        self.assertEqual(poll[2]["state"], "ready")
        self.assertIsNone(poll[2]["running_job_id"])
        self.assertEqual(launched["audio"], b"ID3-audio-bytes")
        command = launched["command"]
        self.assertEqual(command[command.index("--device") + 1], "cuda")
        self.assertEqual(command[command.index("--language") + 1], "ru")
        self.assertEqual(command[command.index("--threads") + 1], str(tw.thread_limit(tw.capabilities(GPU))))
        self.assertIn("--parent-pid", command)
        self.assertEqual(launched["kwargs"]["env"]["OMP_NUM_THREADS"], command[command.index("--threads") + 1])
        complete = [item for item in self.requests if item[1].endswith("/complete")]
        self.assertEqual(len(complete), 1)
        self.assertEqual(complete[0][2]["lines"], [{"start": 1.0, "end": 2.5, "text": "первая"}])
        self.assertEqual(complete[0][2]["device"], "cuda")
        self.assertNotIn(("POST", "/agent/transcription/jobs/7/fail"), self.paths())
        self.assertFalse((worker.runtime.jobs / "7").exists())  # temp audio removed
        self.assertEqual(worker.snapshot()["state"], "idle")

    def test_cpu_only_pc_uses_cpu_device(self):
        worker = self.worker(lambda *a, **k: FakeProcess(), gpu=NO_GPU)
        worker._gpu = NO_GPU
        command = worker.runner_command(self.job, Path("in.mp3"), Path("out.json"), Path("w"), 2)
        self.assertEqual(command[command.index("--device") + 1], "cpu")

    def test_runner_failure_is_reported_for_reassignment(self):
        worker = self.worker(lambda *a, **k: FakeProcess(stderr=["noise\n", "ERROR RuntimeError: CUDA out of memory\n"], returncode=2))
        with httpx.Client(transport=httpx.MockTransport(self.handler)) as client:
            self.assertEqual(worker.process(client, self.job), "failed")
        fail = [item for item in self.requests if item[1].endswith("/fail")]
        self.assertEqual(len(fail), 1)
        self.assertIn("CUDA out of memory", fail[0][2]["reason"])
        self.assertIsNone(worker._running_job)

    def test_bad_media_path_and_empty_result_fail_safely(self):
        worker = self.worker(lambda *a, **k: FakeProcess())
        with httpx.Client(transport=httpx.MockTransport(self.handler)) as client:
            self.assertEqual(worker.process(client, {**self.job, "media_path": "https://evil.example/x"}), "failed")
            self.assertEqual(self.requests[-1][2]["reason"], "invalid_media_path")

            def popen(command, **kwargs):
                Path(command[command.index("--output") + 1]).write_text('{"lines": []}', encoding="utf-8")
                return FakeProcess()
            worker.popen = popen
            self.assertEqual(worker.process(client, self.job), "failed")
            self.assertEqual(self.requests[-1][2]["reason"], "no_speech_detected")

    def test_lost_lease_kills_runner_and_does_not_report(self):
        processes = []

        def popen(command, **kwargs):
            processes.append(FakeProcess(polls_before_exit=10_000))
            return processes[-1]

        worker = self.worker(popen)
        self.progress_status = 409
        with httpx.Client(transport=httpx.MockTransport(self.handler)) as client:
            self.assertEqual(worker.process(client, self.job), "lease_lost")
        self.assertEqual(processes, [])  # lease lost before the runner even started
        self.progress_status = 200
        ticks = iter(range(0, 10_000, 30))
        with patch.object(tw.time, "monotonic", side_effect=lambda: next(ticks)):
            with httpx.Client(transport=httpx.MockTransport(self.handler)) as client:
                def flip(*_):
                    self.progress_status = 409
                worker.sleep = flip
                self.assertEqual(worker.process(client, self.job), "lease_lost")
        self.assertTrue(processes[0].killed)
        self.assertFalse(any(path.endswith("/fail") for _, path in self.paths()))

    def test_disabled_worker_reports_off_once_and_never_installs(self):
        worker = self.worker(lambda *a, **k: FakeProcess(), enabled=False)
        worker.runtime.env_ready = False
        with patch.object(tw.psutil, "cpu_percent", return_value=1.0), \
             patch.object(worker.runtime, "install", side_effect=AssertionError("must not install")):
            worker.run_forever()
        self.assertEqual(len(self.requests), 1)
        self.assertFalse(self.requests[0][2]["enabled"])
        self.assertIsNone(worker._install_thread)

    def test_first_enable_installs_runtime_and_reports_installing(self):
        worker = self.worker(lambda *a, **k: FakeProcess())
        worker.runtime.env_ready = False
        started = threading.Event(); release = threading.Event()

        def install(gpu, report):
            report("шаг 1")
            started.set(); release.wait(5)
        with patch.object(worker.runtime, "install", side_effect=install), \
             patch.object(tw.psutil, "cpu_percent", return_value=1.0):
            worker._gpu = GPU
            worker.ensure_runtime()
            started.wait(5)
            body = worker.poll_body()
            self.assertEqual(body["state"], "installing")
            release.set(); worker._install_thread.join(5)
        worker.runtime.env_ready = True
        self.assertEqual(worker.runtime_state()[0], "ready")

    def test_non_individual_key_or_missing_server_does_nothing(self):
        worker = tw.TranscriptionWorker({"server_url": "https://x", "api_key": "global", tw.CONFIG_KEY: True}, self.root, self.root,
                                        client_factory=lambda: (_ for _ in ()).throw(AssertionError("no network")))
        worker.run_forever()


class RunnerTests(unittest.TestCase):
    def test_build_lines_splits_on_gaps_length_and_sentence_ends(self):
        words = [SimpleNamespace(start=0.0, end=0.4, word=" Я"), SimpleNamespace(start=0.4, end=0.8, word=" иду"),
                 SimpleNamespace(start=2.0, end=2.3, word=" домой."),  # gap > 0.9 s
                 SimpleNamespace(start=2.4, end=2.9, word=" " + "очень" * 12)]  # too long for the line
        lines = runner.build_lines([SimpleNamespace(start=0, end=3, text="", words=words),
                                    {"start": 5, "end": 6, "text": " без слов ", "words": []}])
        self.assertEqual([line["text"] for line in lines], ["Я иду", "домой.", "очень" * 12, "без слов"])
        self.assertEqual((lines[0]["start"], lines[0]["end"]), (0.0, 0.8))
        self.assertEqual(lines[3], {"start": 5.0, "end": 6.0, "text": "без слов"})

    def test_hallucinations_and_repetition_loops_are_dropped(self):
        rows = [{"start": i, "end": i + 1, "text": "ла-ла"} for i in range(6)]
        rows.append({"start": 10, "end": 11, "text": "Субтитры сделал DimaTorzok"})
        cleaned = runner.clean_lines(rows)
        self.assertEqual(len(cleaned), 3)
        self.assertNotIn("DimaTorzok", " ".join(line["text"] for line in cleaned))

    def fake_modules(self, *, cuda_fails=False):
        calls = {"demucs": [], "whisper": []}

        def demucs_main(args):
            calls["demucs"].append(args)
            out = Path(args[args.index("-o") + 1]) / "htdemucs" / Path(args[-1]).stem
            out.mkdir(parents=True, exist_ok=True)
            (out / "vocals.wav").write_bytes(b"RIFF")

        class WhisperModel:
            def __init__(self, name, device, compute_type, cpu_threads, download_root):
                calls["whisper"].append((name, device, compute_type, cpu_threads))
                if cuda_fails and device == "cuda":
                    raise RuntimeError("CUDA failed with error out of memory")

            def transcribe(self, path, **kwargs):
                calls["transcribe"] = kwargs
                words = [SimpleNamespace(start=1.0, end=1.5, word=" Привет"), SimpleNamespace(start=1.5, end=2.0, word=" мир")]
                return iter([SimpleNamespace(start=1.0, end=2.0, text="Привет мир", words=words)]), SimpleNamespace(duration=10, language="ru")

        modules = {"torch": types.SimpleNamespace(set_num_threads=lambda n: calls.setdefault("threads", n)),
                   "demucs": types.ModuleType("demucs"), "demucs.separate": types.SimpleNamespace(main=demucs_main),
                   "faster_whisper": types.SimpleNamespace(WhisperModel=WhisperModel)}
        return modules, calls

    def run_main(self, device, **kw):
        modules, calls = self.fake_modules(**kw)
        with tempfile.TemporaryDirectory() as temp, patch.dict(sys.modules, modules), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            root = Path(temp)
            (root / "in.mp3").write_bytes(b"ID3")
            code = runner.main(["--input", str(root / "in.mp3"), "--output", str(root / "out.json"), "--language", "ru",
                                "--device", device, "--threads", "3", "--models", str(root / "m"), "--workdir", str(root / "w")])
            result = json.loads((root / "out.json").read_text("utf-8")) if code == 0 else None
        return code, result, calls

    def test_runner_demucs_vocals_then_whisper_large_v3_float16_on_gpu(self):
        code, result, calls = self.run_main("cuda")
        self.assertEqual(code, 0)
        args = calls["demucs"][0]
        self.assertEqual(args[args.index("-n") + 1], "htdemucs")
        self.assertEqual(args[args.index("--two-stems") + 1], "vocals")
        self.assertEqual(calls["whisper"], [("large-v3", "cuda", "float16", 3)])
        self.assertEqual(calls["transcribe"]["language"], "ru")
        self.assertTrue(calls["transcribe"]["word_timestamps"])
        self.assertEqual(calls["threads"], 3)
        self.assertEqual(result["lines"], [{"start": 1.0, "end": 2.0, "text": "Привет мир"}])
        self.assertEqual((result["model"], result["device"]), ("large-v3", "cuda"))

    def test_runner_cpu_int8_and_cuda_failure_falls_back_to_cpu(self):
        code, result, calls = self.run_main("cpu")
        self.assertEqual(calls["whisper"], [("large-v3", "cpu", "int8", 3)])
        code, result, calls = self.run_main("cuda", cuda_fails=True)
        self.assertEqual(code, 0)
        self.assertEqual([item[1:3] for item in calls["whisper"]], [("cuda", "float16"), ("cpu", "int8")])
        self.assertEqual(result["device"], "cpu")

    def test_runner_error_exit_code(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(sys.modules, {"torch": None}), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = runner.main(["--input", str(Path(temp) / "missing.mp3"), "--output", str(Path(temp) / "o.json"),
                                "--models", temp, "--workdir", temp])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Run every test_*.py module in a fresh bounded Python process.

Isolation prevents Tk/native-library teardown in one module from corrupting a
later module. No test/module exclusion is supported. Failures, import failures,
crashes, empty modules and timeouts fail the suite and remain in the JSON report.
"""
from __future__ import annotations

import argparse
import faulthandler
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest

COUNTS = ("tests", "skipped", "failures", "errors", "expected_failures", "unexpected_successes")
DEFAULT_ROOT = Path(__file__).resolve().parents[1]


def discover_modules(tests: Path) -> list[Path]:
    tests = tests.resolve(strict=True)
    modules = sorted(tests.rglob("test_*.py"))
    if not modules:
        raise ValueError("No test_*.py modules found")
    for module in modules:
        if not module.is_file() or not module.resolve().is_relative_to(tests):
            raise ValueError("Unsafe test module path")
        if any(part.is_symlink() for part in (module, *module.parents) if part != tests.parent):
            raise ValueError("Test module paths must not contain links")
    return modules


def worker(module: Path, result_path: Path, root: Path, timeout: float) -> int:
    sys.path.insert(0, str(root))
    sys.path.insert(0, str(module.parent))
    faulthandler.enable()
    faulthandler.dump_traceback_later(max(.1, min(90.0, timeout / 2)), repeat=True)
    try:
        try:
            # Import exactly this file. Recursive discovery here would run a
            # same-named nested module twice and defeat per-module isolation.
            spec = importlib.util.spec_from_file_location(module.stem, module)
            imported = importlib.util.module_from_spec(spec)
            sys.modules[module.stem] = imported
            spec.loader.exec_module(imported)
            suite = unittest.defaultTestLoader.loadTestsFromModule(imported)
        except Exception as error:
            def failed_import(error=error):
                raise error
            suite = unittest.TestSuite([unittest.FunctionTestCase(failed_import,
                                        description=f"Import failed: {module.name}")])
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        report = {
            "tests": result.testsRun, "skipped": len(result.skipped),
            "failures": len(result.failures), "errors": len(result.errors),
            "expected_failures": len(result.expectedFailures),
            "unexpected_successes": len(result.unexpectedSuccesses),
        }
        result_path.write_text(json.dumps(report), encoding="utf-8")
        return 0 if result.wasSuccessful() and result.testsRun > 0 else 1
    finally:
        faulthandler.cancel_dump_traceback_later()


def stop_process_tree(child: subprocess.Popen) -> None:
    """Only terminate the process tree created for this exact test module."""
    if child.poll() is not None:
        return
    if os.name == "posix":
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    else:
        try:
            subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=10, check=False)
        except (OSError, subprocess.TimeoutExpired):
            pass
        if child.poll() is None:
            child.kill()


def run_module(module: Path, root: Path, timeout: float, *, output, max_output: int) -> dict:
    started = time.monotonic()
    record = {"outcome": "CRASH", "exit_code": None, **{key: 0 for key in COUNTS}}
    flags = set()
    with tempfile.TemporaryDirectory(prefix="xass-module-result-") as temporary:
        result_path = Path(temporary) / "result.json"
        command = [sys.executable, "-u", "-X", "utf8", "-B", str(Path(__file__).resolve()),
                   "--worker-module", str(module), "--worker-result", str(result_path),
                   "--repo-root", str(root), "--module-timeout", str(timeout)]
        env = dict(os.environ, PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1")
        try:
            child = subprocess.Popen(command, cwd=root, env=env, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     start_new_session=os.name == "posix")
        except OSError:
            record["outcome"] = "START_FAILED"
            record["duration_seconds"] = round(time.monotonic() - started, 3)
            return record

        def stream():
            count = 0
            try:
                while True:
                    block = child.stdout.read(min(8192, max_output - count + 1))
                    if not block:
                        return
                    count += len(block)
                    if count > max_output:
                        flags.add("OUTPUT_LIMIT")
                        stop_process_tree(child)
                        return
                    output.write(block.decode("utf-8", errors="replace"))
                    output.flush()
            except (OSError, ValueError):
                flags.add("OUTPUT_FAILED")
                stop_process_tree(child)

        reader = threading.Thread(target=stream, daemon=True)
        reader.start()
        try:
            child.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            flags.add("TIMEOUT")
            stop_process_tree(child)
            child.wait(timeout=15)
        reader.join(timeout=3)
        if reader.is_alive():
            flags.add("OUTPUT_FAILED")
        else:
            child.stdout.close()
        record["exit_code"] = child.returncode
        counts_valid = False
        if result_path.is_file() and result_path.stat().st_size <= 8192:
            try:
                counts = json.loads(result_path.read_text(encoding="utf-8"))
                if (isinstance(counts, dict) and set(counts) == set(COUNTS)
                        and all(type(counts[key]) is int and 0 <= counts[key] <= 1_000_000 for key in COUNTS)):
                    record.update(counts)
                    counts_valid = True
            except (ValueError, OSError):
                pass
        if flags:
            record["outcome"] = next(flag for flag in ("TIMEOUT", "OUTPUT_LIMIT", "OUTPUT_FAILED") if flag in flags)
        elif not counts_valid:
            record["outcome"] = "CRASH" if child.returncode else "MISSING_RESULT"
        elif not record["tests"]:
            record["outcome"] = "NO_TESTS"
        elif child.returncode == 0 and not (record["failures"] or record["errors"] or record["unexpected_successes"]):
            record["outcome"] = "PASS"
        else:
            record["outcome"] = "FAIL"
    record["duration_seconds"] = round(time.monotonic() - started, 3)
    return record


def run_suite(tests: Path, root: Path, *, module_timeout: float = 240, suite_timeout: float = 1200,
              max_output: int = 2 * 1024 * 1024, output=sys.stdout) -> dict:
    tests, root = tests.resolve(strict=True), root.resolve(strict=True)
    modules = discover_modules(tests)
    started = time.monotonic()
    records = []
    for index, module in enumerate(modules, 1):
        name = module.relative_to(tests).as_posix()
        output.write(f"\n=== [{index}/{len(modules)}] {name} (isolated process) ===\n")
        output.flush()
        remaining = suite_timeout - (time.monotonic() - started)
        if remaining <= 0:
            record = {"outcome": "NOT_RUN_SUITE_TIMEOUT", "exit_code": None,
                      "duration_seconds": 0, **{key: 0 for key in COUNTS}}
        else:
            record = run_module(module, root, min(module_timeout, remaining), output=output, max_output=max_output)
        records.append({"module": name, **record})
        output.write(f"=== {name}: {record['outcome']} ({record['tests']} tests, {record['skipped']} skipped) ===\n")
        output.flush()
    return {
        "schema": 1, "success": all(record["outcome"] == "PASS" for record in records),
        "complete": all(record["outcome"] != "NOT_RUN_SUITE_TIMEOUT" for record in records),
        "discovered_modules": len(modules), "modules": records,
        "totals": {key: sum(record[key] for record in records) for key in COUNTS},
        "duration_seconds": round(time.monotonic() - started, 3),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tests-dir", type=Path, default=DEFAULT_ROOT / "tests")
    parser.add_argument("--repo-root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--module-timeout", type=float, default=240)
    parser.add_argument("--suite-timeout", type=float, default=1200)
    parser.add_argument("--max-output-bytes", type=int, default=2 * 1024 * 1024)
    parser.add_argument("--worker-module", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--worker-result", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if not (0 < args.module_timeout <= 600 and 0 < args.suite_timeout <= 3600
            and 1024 <= args.max_output_bytes <= 16 * 1024 * 1024):
        parser.error("Timeouts/output limit are outside the allowed bounds")
    if args.worker_module is not None:
        if args.worker_result is None:
            parser.error("Worker result path is required")
        return worker(args.worker_module.resolve(strict=True), args.worker_result, args.repo_root, args.module_timeout)
    report = run_suite(args.tests_dir, args.repo_root, module_timeout=args.module_timeout,
                       suite_timeout=args.suite_timeout, max_output=args.max_output_bytes)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "modules"}, sort_keys=True))
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

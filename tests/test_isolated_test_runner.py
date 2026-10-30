from __future__ import annotations

import importlib.util
import io
from pathlib import Path
import tempfile
import textwrap
import unittest

SPEC = importlib.util.spec_from_file_location("isolated_runner", Path(__file__).resolve().parents[1] / "scripts/run_tests_isolated.py")
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


class IsolatedRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="xass-runner-fixture-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def write(self, name, source):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(source), encoding="utf-8")
        return path

    def run_fixture(self, **options):
        return runner.run_suite(self.root, self.root, output=io.StringIO(), module_timeout=10,
                                suite_timeout=30, **options)

    def test_all_modules_run_and_failure_does_not_hide_later_success(self):
        self.write("test_a_failure.py", """
            import unittest
            class Case(unittest.TestCase):
                def test_failure(self): self.fail('expected fixture failure')
        """)
        self.write("nested/test_b_success.py", """
            import unittest
            class Case(unittest.TestCase):
                def test_success(self): self.assertTrue(True)
        """)
        result = self.run_fixture()
        self.assertEqual(result["discovered_modules"], 2)
        self.assertEqual({row["module"]: row["outcome"] for row in result["modules"]},
                         {"test_a_failure.py": "FAIL", "nested/test_b_success.py": "PASS"})
        self.assertFalse(result["success"])
        self.assertTrue(result["complete"])
        self.assertEqual(result["totals"]["tests"], 2)
        self.assertEqual(result["totals"]["failures"], 1)

    def test_global_module_state_does_not_leak_into_next_process(self):
        self.write("test_a_state.py", """
            import builtins, unittest
            builtins.XASS_ISOLATION_SENTINEL = True
            class Case(unittest.TestCase):
                def test_state(self): self.assertTrue(builtins.XASS_ISOLATION_SENTINEL)
        """)
        self.write("test_b_clean.py", """
            import builtins, unittest
            class Case(unittest.TestCase):
                def test_clean(self): self.assertFalse(hasattr(builtins, 'XASS_ISOLATION_SENTINEL'))
        """)
        self.assertTrue(self.run_fixture()["success"])

    def test_crash_import_error_and_empty_module_fail_explicitly(self):
        self.write("test_a_crash.py", "import os; os._exit(7)")
        self.write("test_b_import.py", "raise RuntimeError('fixture import error')")
        self.write("test_c_empty.py", "pass")
        result = self.run_fixture()
        self.assertEqual([row["outcome"] for row in result["modules"]], ["CRASH", "FAIL", "NO_TESTS"])
        self.assertFalse(result["success"])
        self.assertEqual(result["totals"]["errors"], 1)

    def test_timeout_is_a_failure_not_a_skip(self):
        module = self.write("test_wait.py", "import time; time.sleep(20)")
        result = runner.run_module(module, self.root, .2, output=io.StringIO(), max_output=4096)
        self.assertEqual(result["outcome"], "TIMEOUT")
        self.assertEqual(result["skipped"], 0)

    def test_output_limit_is_a_failure(self):
        module = self.write("test_noisy.py", "print('x' * 20000)")
        result = runner.run_module(module, self.root, 10, output=io.StringIO(), max_output=1024)
        self.assertEqual(result["outcome"], "OUTPUT_LIMIT")

    def test_skips_and_expected_failures_are_reported_separately(self):
        self.write("test_results.py", """
            import unittest
            class Case(unittest.TestCase):
                @unittest.skip('fixture platform')
                def test_skip(self): pass
                @unittest.expectedFailure
                def test_expected(self): self.fail('fixture expected failure')
                def test_pass(self): pass
        """)
        result = self.run_fixture()
        self.assertTrue(result["success"])
        self.assertEqual(result["totals"]["tests"], 3)
        self.assertEqual(result["totals"]["skipped"], 1)
        self.assertEqual(result["totals"]["expected_failures"], 1)

    def test_no_modules_is_not_a_false_green(self):
        with self.assertRaisesRegex(ValueError, "No test"):
            runner.discover_modules(self.root)


if __name__ == "__main__":
    unittest.main()

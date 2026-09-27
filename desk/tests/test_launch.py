"""The double-click launcher.

This code runs before anything is installed and is read by someone who has no
terminal open, so its failure messages are part of the product. The tests check
that it recovers where it can, and explains itself where it cannot.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import launch

DESK = Path(__file__).resolve().parent.parent


class LaunchCase(unittest.TestCase):
    """Redirects the module's paths at a scratch directory."""

    def setUp(self):
        self.scratch = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.scratch, ignore_errors=True)
        self.venv = self.scratch / ".venv"
        self.venv.mkdir()
        self.requirements = self.scratch / "requirements.txt"
        self.requirements.write_text("fastapi>=0.115\n")
        self.stamp = self.venv / ".requirements-stamp"
        patches = {
            "VENV_DIR": self.venv,
            "REQUIREMENTS": self.requirements,
            "STAMP": self.stamp,
        }
        for name, value in patches.items():
            patcher = mock.patch.object(launch, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)


class TestPythonVersion(unittest.TestCase):
    def test_the_running_python_is_accepted(self):
        launch.check_python_version()   # must not raise on a supported version

    def test_an_old_python_is_refused_with_somewhere_to_go(self):
        with mock.patch.object(sys, "version_info", (3, 8, 0)):
            with self.assertRaises(launch.SetupError) as caught:
                launch.check_python_version()
        message = str(caught.exception)
        self.assertIn("3.10", message)
        self.assertIn("python.org/downloads", message)

    def test_the_message_names_the_version_actually_found(self):
        with mock.patch.object(sys, "version_info", (3, 9, 7)):
            with self.assertRaises(launch.SetupError) as caught:
                launch.check_python_version()
        self.assertIn("3.9.7", str(caught.exception))


class TestVenvLocation(unittest.TestCase):
    def test_the_interpreter_path_matches_the_platform(self):
        path = launch.venv_python()
        if os.name == "nt":
            self.assertEqual(path.parent.name, "Scripts")
            self.assertEqual(path.name, "python.exe")
        else:
            self.assertEqual(path.parent.name, "bin")
            self.assertEqual(path.name, "python")

    def test_the_environment_sits_beside_the_launcher(self):
        self.assertEqual(launch.VENV_DIR.parent, DESK)


class TestRequirementsStamp(LaunchCase):
    def test_no_stamp_means_not_installed(self):
        self.assertFalse(launch.dependencies_are_current())

    def test_a_matching_stamp_means_installed(self):
        self.stamp.write_text(launch.requirements_fingerprint())
        self.assertTrue(launch.dependencies_are_current())

    def test_changing_requirements_invalidates_the_stamp(self):
        self.stamp.write_text(launch.requirements_fingerprint())
        self.requirements.write_text("fastapi>=0.115\nanthropic>=0.40\n")
        self.assertFalse(
            launch.dependencies_are_current(),
            "a dependency added later must actually get installed",
        )

    def test_a_corrupt_stamp_is_treated_as_missing(self):
        self.stamp.write_bytes(b"\xff\xfe not text")
        self.assertFalse(launch.dependencies_are_current())

    def test_whitespace_in_the_stamp_is_tolerated(self):
        self.stamp.write_text(f"  {launch.requirements_fingerprint()}\n")
        self.assertTrue(launch.dependencies_are_current())


class TestEnvironmentCheck(LaunchCase):
    def test_a_nonexistent_interpreter_is_not_usable(self):
        self.assertFalse(launch.environment_is_usable(self.scratch / "nope"))

    def test_the_running_interpreter_can_import_the_standard_library(self):
        with mock.patch.object(launch, "REQUIRED_MODULES", ("json", "pathlib")):
            self.assertTrue(launch.environment_is_usable(Path(sys.executable)))

    def test_missing_modules_are_reported_as_unusable(self):
        with mock.patch.object(launch, "REQUIRED_MODULES", ("definitely_not_a_module",)):
            self.assertFalse(launch.environment_is_usable(Path(sys.executable)))


class TestRepair(LaunchCase):
    def test_a_working_environment_is_left_alone(self):
        with mock.patch.object(launch, "install_dependencies") as install, \
             mock.patch.object(launch, "environment_is_usable", return_value=True):
            launch.prepare_environment(self.scratch / "python")
        self.assertEqual(install.call_count, 1)

    def test_a_broken_environment_is_reinstalled_once(self):
        usable = iter([False, True])
        with mock.patch.object(launch, "install_dependencies") as install, \
             mock.patch.object(launch, "environment_is_usable",
                               side_effect=lambda _: next(usable)):
            launch.prepare_environment(self.scratch / "python")
        self.assertEqual(install.call_count, 2, "should install, notice, reinstall")

    def test_the_stamp_is_cleared_before_reinstalling(self):
        self.stamp.write_text(launch.requirements_fingerprint())
        usable = iter([False, True])
        seen = []
        with mock.patch.object(launch, "install_dependencies",
                               side_effect=lambda _: seen.append(self.stamp.exists())), \
             mock.patch.object(launch, "environment_is_usable",
                               side_effect=lambda _: next(usable)):
            launch.prepare_environment(self.scratch / "python")
        self.assertEqual(seen[-1], False, "the reinstall must not be skipped again")

    def test_an_unrepairable_environment_says_what_to_delete(self):
        with mock.patch.object(launch, "install_dependencies"), \
             mock.patch.object(launch, "environment_is_usable", return_value=False):
            with self.assertRaises(launch.SetupError) as caught:
                launch.prepare_environment(self.scratch / "python")
        self.assertIn(str(self.venv), str(caught.exception))


class TestMain(LaunchCase):
    def test_setup_failure_exits_non_zero_so_the_window_stays_open(self):
        with mock.patch.object(launch, "check_python_version",
                               side_effect=launch.SetupError("no Python here")):
            self.assertEqual(launch.main([]), 1)

    def test_the_failure_is_printed_where_someone_can_read_it(self):
        import contextlib
        import io
        buffer = io.StringIO()
        with mock.patch.object(launch, "check_python_version",
                               side_effect=launch.SetupError("no Python here")):
            with contextlib.redirect_stdout(buffer):
                launch.main([])
        output = buffer.getvalue()
        self.assertIn("could not start", output)
        self.assertIn("no Python here", output)

    def test_arguments_are_passed_through_to_the_app(self):
        with mock.patch.object(launch, "check_python_version"), \
             mock.patch.object(launch, "ensure_venv", return_value=Path("py")), \
             mock.patch.object(launch, "prepare_environment"), \
             mock.patch.object(launch, "run_app", return_value=0) as run:
            launch.main(["--port", "9999", "--no-browser"])
        self.assertEqual(run.call_args[0][1], ["--port", "9999", "--no-browser"])

    def test_the_apps_exit_code_is_returned(self):
        with mock.patch.object(launch, "check_python_version"), \
             mock.patch.object(launch, "ensure_venv", return_value=Path("py")), \
             mock.patch.object(launch, "prepare_environment"), \
             mock.patch.object(launch, "run_app", return_value=3):
            self.assertEqual(launch.main([]), 3)


class TestWrapperScripts(unittest.TestCase):
    """The files someone actually double-clicks."""

    def test_both_wrappers_exist(self):
        self.assertTrue((DESK / "Start YM Desk.bat").is_file())
        self.assertTrue((DESK / "Start YM Desk.command").is_file())

    def test_the_unix_wrapper_is_executable(self):
        self.assertTrue(os.access(DESK / "Start YM Desk.command", os.X_OK))

    def test_the_unix_wrapper_is_valid_shell(self):
        result = subprocess.run(
            ["sh", "-n", str(DESK / "Start YM Desk.command")],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_both_wrappers_move_to_their_own_folder_first(self):
        # Double-clicking does not set the working directory.
        self.assertIn('cd /d "%~dp0"', (DESK / "Start YM Desk.bat").read_text())
        self.assertIn('cd "$(dirname "$0")"',
                      (DESK / "Start YM Desk.command").read_text())

    def test_both_wrappers_hand_over_to_the_launcher(self):
        self.assertIn("launch.py", (DESK / "Start YM Desk.bat").read_text())
        self.assertIn("launch.py", (DESK / "Start YM Desk.command").read_text())

    def test_both_wrappers_pause_on_failure(self):
        # Otherwise the window vanishes and takes the error message with it.
        self.assertIn("pause", (DESK / "Start YM Desk.bat").read_text())
        self.assertIn("read -r", (DESK / "Start YM Desk.command").read_text())

    def test_the_windows_wrapper_prefers_the_py_launcher(self):
        # Plain "python" on Windows is often the Store stub, which runs nothing.
        text = (DESK / "Start YM Desk.bat").read_text()
        self.assertLess(text.index("py -3"), text.index("set \"PYTHON=python\""))

    def test_the_wrappers_send_arguments_on(self):
        self.assertIn("%*", (DESK / "Start YM Desk.bat").read_text())
        self.assertIn('"$@"', (DESK / "Start YM Desk.command").read_text())


if __name__ == "__main__":
    unittest.main()


class TestProjectLayout(unittest.TestCase):
    """The desk is half a project; the other half has to come with it."""

    def test_the_real_checkout_passes(self):
        launch.check_layout()   # must not raise in a complete project

    def test_a_missing_engine_is_explained_not_traced(self):
        missing = Path(tempfile.mkdtemp()) / "trading" / "ym"
        with mock.patch.object(launch, "ENGINE", missing):
            with self.assertRaises(launch.SetupError) as caught:
                launch.check_layout()
        message = str(caught.exception)
        self.assertIn("side by side", message)
        self.assertIn("desk", message)

    def test_the_layout_is_checked_before_anything_is_built(self):
        # Otherwise the first sign of trouble is a venv built for nothing.
        order = []
        with mock.patch.object(launch, "check_layout",
                               side_effect=lambda: order.append("layout")), \
             mock.patch.object(launch, "check_python_version",
                               side_effect=lambda: order.append("python")), \
             mock.patch.object(launch, "ensure_venv",
                               side_effect=lambda: order.append("venv") or Path("py")), \
             mock.patch.object(launch, "prepare_environment"), \
             mock.patch.object(launch, "run_app", return_value=0):
            launch.main([])
        self.assertEqual(order, ["layout", "python", "venv"])

    def test_the_hub_refuses_to_import_without_the_engine(self):
        import importlib
        import hub.config as config
        with mock.patch.object(config, "TRADING_ROOT", Path("/nowhere/at/all")):
            with self.assertRaises(RuntimeError) as caught:
                config.bootstrap_engine_path()
        self.assertIn("side by side", str(caught.exception))

"""First-run setup and launch, so the app can be started by double-clicking.

Run by the ``Start YM Desk`` wrappers with whatever Python is on the system. It
creates a private virtual environment beside this file, installs what the app
needs into it, and then hands over to that environment to run the app.

Everything here uses only the standard library, because it has to work *before*
anything is installed. Errors are reported as plain sentences with the thing to
do next, since the person reading them double-clicked an icon and has no
terminal to investigate with.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import venv
from pathlib import Path

HERE = Path(__file__).resolve().parent
VENV_DIR = HERE / ".venv"
REQUIREMENTS = HERE / "requirements.txt"
STAMP = VENV_DIR / ".requirements-stamp"
MINIMUM_PYTHON = (3, 10)


class SetupError(Exception):
    """Something the person needs to fix, phrased for someone without a terminal."""


def say(message: str = "") -> None:
    print(message, flush=True)


def venv_python() -> Path:
    """The interpreter inside our virtual environment."""
    if os.name == "nt":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def check_python_version() -> None:
    if sys.version_info < MINIMUM_PYTHON:
        current = ".".join(str(part) for part in sys.version_info[:3])
        wanted = ".".join(str(part) for part in MINIMUM_PYTHON)
        raise SetupError(
            f"This needs Python {wanted} or newer, but the Python that started it "
            f"is {current}.\n\n"
            f"Install a current version from https://www.python.org/downloads/ "
            f"and run this again.\n"
            f"(On Windows, tick 'Add python.exe to PATH' in the installer.)"
        )


def ensure_venv() -> Path:
    """Create the virtual environment if it is missing. Returns its interpreter."""
    interpreter = venv_python()
    if interpreter.exists():
        return interpreter

    say("Setting up for first use. This takes a minute or two, once.")
    say()
    try:
        venv.EnvBuilder(with_pip=True, clear=False).create(VENV_DIR)
    except Exception as exc:  # noqa: BLE001 - surfaced to a non-technical reader
        hint = ""
        if sys.platform.startswith("linux"):
            hint = (
                "\n\nOn Debian or Ubuntu this usually means the venv module is not "
                "installed. Try:\n    sudo apt install python3-venv"
            )
        raise SetupError(
            f"Could not create the private Python environment in:\n  {VENV_DIR}"
            f"\n\nThe reason given was: {exc}{hint}"
        ) from exc

    if not interpreter.exists():
        raise SetupError(
            f"The Python environment was created but has no interpreter at:\n"
            f"  {interpreter}\nDelete the .venv folder and try again."
        )
    return interpreter


def requirements_fingerprint() -> str:
    return hashlib.sha256(REQUIREMENTS.read_bytes()).hexdigest()


def dependencies_are_current() -> bool:
    """Have we already installed exactly this requirements file?

    Comparing a hash rather than just checking that one import works means a
    dependency added later is actually installed, instead of silently skipped.
    """
    if not STAMP.exists():
        return False
    try:
        return STAMP.read_text().strip() == requirements_fingerprint()
    except (OSError, ValueError):
        # Unreadable or not text (UnicodeDecodeError is a ValueError). Either
        # way we cannot trust it, and reinstalling is cheap next to crashing.
        return False


def install_dependencies(interpreter: Path) -> None:
    if dependencies_are_current():
        return
    if not REQUIREMENTS.exists():
        raise SetupError(f"Cannot find {REQUIREMENTS.name} next to this file.")

    say("Installing what the app needs. Give it a minute -- it stays quiet")
    say("while it works, and only does this when the requirements change.")
    say()
    # Quiet on purpose: the person reading this double-clicked an icon, and
    # pip's full output looks like something going wrong. Errors still show.
    command = [
        str(interpreter), "-m", "pip", "install", "--disable-pip-version-check",
        "--quiet", "--progress-bar", "off", "-r", str(REQUIREMENTS),
    ]
    result = subprocess.run(command, cwd=HERE)
    if result.returncode != 0:
        raise SetupError(
            "Could not install the app's dependencies.\n\n"
            "The most common cause is no internet connection -- the first run "
            "needs one, later runs do not.\n"
            f"The command that failed was:\n  {' '.join(command)}"
        )
    STAMP.write_text(requirements_fingerprint())
    say()


# Imported by run.py's process, so a broken environment shows up here rather
# than as a traceback after the window has already said it was starting.
REQUIRED_MODULES = ("fastapi", "uvicorn", "multipart")


def environment_is_usable(interpreter: Path) -> bool:
    """Can the virtual environment actually import what the app needs?"""
    probe = "import " + ", ".join(REQUIRED_MODULES)
    try:
        result = subprocess.run(
            [str(interpreter), "-c", probe],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except OSError:
        return False  # the interpreter itself is gone or will not start
    return result.returncode == 0


def prepare_environment(interpreter: Path) -> None:
    """Install dependencies, and repair the environment if it is broken.

    The stamp says whether we have installed *this* requirements file, but it
    cannot know whether the result still works -- a half-deleted or upgraded
    environment passes that check and fails at import. So the stamp decides
    whether to install, and an actual import decides whether that worked.
    """
    install_dependencies(interpreter)
    if environment_is_usable(interpreter):
        return

    say("The installed files look incomplete. Repairing...")
    say()
    STAMP.unlink(missing_ok=True)
    install_dependencies(interpreter)
    if not environment_is_usable(interpreter):
        raise SetupError(
            "The app's dependencies are installed but cannot be loaded.\n\n"
            f"Deleting this folder and running again usually fixes it:\n"
            f"  {VENV_DIR}"
        )


def run_app(interpreter: Path, argv: list[str]) -> int:
    """Hand over to the virtual environment's Python to run the app."""
    command = [str(interpreter), str(HERE / "run.py"), *argv]
    try:
        return subprocess.call(command, cwd=HERE)
    except KeyboardInterrupt:
        return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        check_python_version()
        interpreter = ensure_venv()
        prepare_environment(interpreter)
    except SetupError as exc:
        say()
        say("=" * 68)
        say("YM Desk could not start.")
        say("=" * 68)
        say()
        say(str(exc))
        say()
        return 1
    return run_app(interpreter, argv)


if __name__ == "__main__":
    raise SystemExit(main())

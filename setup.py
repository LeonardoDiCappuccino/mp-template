"""Everything is configured in pyproject.toml; this only adds a hook to `pip install -e .`.

After the project is built, the MicroPython stubs for IntelliSense are fetched into ./typings
(skipped if the folder already exists). Installing is only allowed inside an activated venv.
"""
import os
import subprocess
import sys
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py
from setuptools.command.editable_wheel import editable_wheel

STUBS = "micropython-esp32-stubs~=1.29.0"
TYPINGS = Path(__file__).resolve().parent / "typings"


def fetch_stubs():
    venv = os.environ.get("VIRTUAL_ENV")
    if not venv:
        sys.exit("error: install into a virtual environment (python -m venv .venv, then activate it)")
    if TYPINGS.exists():
        return
    python = Path(venv) / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    # pip runs this inside an isolated build environment: give pip a clean one again
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTHON", "PIP_"))}
    subprocess.check_call(
        [str(python), "-m", "pip", "install", "--no-user", "--target", str(TYPINGS), STUBS], env=env)


class BuildPy(build_py):
    def run(self):
        super().run()
        fetch_stubs()


class EditableWheel(editable_wheel):
    def run(self):
        super().run()
        fetch_stubs()


setup(cmdclass={"build_py": BuildPy, "editable_wheel": EditableWheel})

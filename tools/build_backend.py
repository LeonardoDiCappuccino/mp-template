"""Build backend for pyproject.toml: setuptools plus one extra step.

After the project is built (`pip install -e .`), the MicroPython stubs for IntelliSense are fetched
into ./typings (skipped if the folder already exists). Installing is only allowed inside an
activated venv.
"""
import os
import subprocess
import sys
from pathlib import Path

from setuptools import build_meta as _orig
from setuptools.build_meta import *  # noqa: F401,F403  (all other hooks stay setuptools')

STUBS = "micropython-esp32-stubs~=1.29.0"
TYPINGS = Path(__file__).resolve().parent.parent / "typings"


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


def build_wheel(*args, **kwargs):
    name = _orig.build_wheel(*args, **kwargs)
    fetch_stubs()
    return name


def build_editable(*args, **kwargs):
    name = _orig.build_editable(*args, **kwargs)
    fetch_stubs()
    return name

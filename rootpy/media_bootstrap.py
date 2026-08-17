from __future__ import annotations

import importlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Optional


def _run_pip(*arguments: str) -> None:
    command = [sys.executable, "-m", "pip", "install", *arguments]
    completed = subprocess.run(command, check=False)
    if completed.returncode != 0:
        raise RuntimeError(
            "Automatic media dependency installation failed: "
            + " ".join(command)
        )
    importlib.invalidate_caches()


def _can_import(name: str) -> bool:
    try:
        importlib.import_module(name)
    except ImportError:
        return False
    return True


def ensure_ffmpeg() -> str:
    """Return an FFmpeg executable, installing a private copy when needed."""
    configured = os.environ.get("ROOTPY_FFMPEG")
    if configured and Path(configured).is_file():
        return configured

    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg:
        os.environ["ROOTPY_FFMPEG"] = system_ffmpeg
        return system_ffmpeg

    if not _can_import("imageio_ffmpeg"):
        print("rootpy: FFmpeg was not found; installing a private copy...")
        _run_pip("--only-binary=:all:", "imageio-ffmpeg>=0.5,<1")

    import imageio_ffmpeg

    executable = imageio_ffmpeg.get_ffmpeg_exe()
    if not executable or not Path(executable).is_file():
        raise RuntimeError("imageio-ffmpeg did not provide an FFmpeg executable")

    executable = str(Path(executable).resolve())
    os.environ["ROOTPY_FFMPEG"] = executable

    directory = str(Path(executable).parent)
    current_path = os.environ.get("PATH", "")
    if directory not in current_path.split(os.pathsep):
        os.environ["PATH"] = directory + os.pathsep + current_path

    return executable


def ensure_media_dependencies() -> Optional[str]:
    """Install the Python 3.9-compatible media stack when it is absent.

    aiortc 1.13 declares PyAV <15, while the available CPython 3.9 Windows
    wheel is PyAV 15.1. rootpy therefore installs aiortc without dependency
    resolution and keeps the binary PyAV wheel already available for 3.9.
    """
    ffmpeg = ensure_ffmpeg()

    if not _can_import("av"):
        print("rootpy: installing the PyAV binary wheel...")
        _run_pip("--only-binary=:all:", "av==15.1.0")

    dependencies = (
        "aioice>=0.10.1,<1",
        "cryptography>=44",
        "google-crc32c>=1.1",
        "pyee>=13",
        "pylibsrtp>=0.10",
        "pyopenssl>=25",
    )
    missing = []
    import_names = (
        "aioice",
        "cryptography",
        "google_crc32c",
        "pyee",
        "pylibsrtp",
        "OpenSSL",
    )
    for requirement, import_name in zip(dependencies, import_names):
        if not _can_import(import_name):
            missing.append(requirement)

    if missing:
        print("rootpy: installing WebRTC support dependencies...")
        _run_pip("--only-binary=:all:", *missing)

    if not _can_import("aiortc"):
        print("rootpy: installing aiortc for Python 3.9...")
        _run_pip("--no-deps", "aiortc==1.13.0")

    try:
        importlib.import_module("av")
        importlib.import_module("aiortc")
    except ImportError as exc:
        raise RuntimeError(
            "The media stack was installed but could not be imported"
        ) from exc

    return ffmpeg

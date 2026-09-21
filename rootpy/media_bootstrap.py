"""Locating the optional media stack.

Voice needs FFmpeg plus the WebRTC stack (``av``, ``aiortc`` and friends).
These are *not* installed automatically. A library that shells out to ``pip``
mid-call breaks in every environment where that is not allowed -- frozen
builds, read-only containers, CI images, managed virtualenvs -- and it does so
with an error that points at pip rather than at rootpy.

Instead these helpers look for what is already present and, when something is
missing, raise :class:`MediaDependencyMissing` naming the exact install
command. Install with ``pip install "rootpy-client[voice]"``.
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path
import shutil
from typing import Optional

from .exceptions import RootError

VOICE_EXTRA = 'pip install "rootpy-client[voice]"'


class MediaDependencyMissing(RootError, RuntimeError):
    """Raised when an optional media dependency is not installed.

    RootError comes first so `except RootError` catches it like everything else
    the library raises; RuntimeError stays in the MRO for existing handlers.
    """

    def __init__(self, what: str, hint: str = VOICE_EXTRA) -> None:
        super().__init__(
            f"{what} is required for voice support but is not installed. "
            f"Install the optional media stack with:\n    {hint}"
        )
        self.what = what
        self.hint = hint


def _can_import(name: str) -> bool:
    try:
        importlib.import_module(name)
    except ImportError:
        return False
    return True


def find_ffmpeg() -> Optional[str]:
    """Return a usable FFmpeg path, or None. Never installs anything.

    Checked in order: ``$ROOTPY_FFMPEG``, ``ffmpeg`` on PATH, then a copy
    provided by ``imageio-ffmpeg`` if that happens to be installed.
    """
    configured = os.environ.get("ROOTPY_FFMPEG")
    if configured and Path(configured).is_file():
        return configured

    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg:
        return system_ffmpeg

    if _can_import("imageio_ffmpeg"):
        import imageio_ffmpeg

        executable = imageio_ffmpeg.get_ffmpeg_exe()
        if executable and Path(executable).is_file():
            return str(Path(executable).resolve())

    return None


def ensure_ffmpeg() -> str:
    """Return an FFmpeg path or raise :class:`MediaDependencyMissing`."""
    executable = find_ffmpeg()
    if executable is None:
        raise MediaDependencyMissing(
            "FFmpeg",
            hint=(
                "install FFmpeg from your package manager, set $ROOTPY_FFMPEG "
                'to its path, or run: pip install "rootpy-client[voice]"'
            ),
        )
    os.environ.setdefault("ROOTPY_FFMPEG", executable)
    return executable


def missing_media_dependencies() -> list:
    """Names of the media imports that are absent. Empty means ready to go."""
    required = ("av", "aiortc")
    return [name for name in required if not _can_import(name)]


def media_stack_available() -> bool:
    """True when FFmpeg and the WebRTC imports are all present."""
    return find_ffmpeg() is not None and not missing_media_dependencies()


def ensure_media_dependencies() -> str:
    """Verify the whole media stack is importable; return the FFmpeg path.

    Raises :class:`MediaDependencyMissing` naming the first missing piece.
    Kept for backwards compatibility -- it no longer installs anything.
    """
    executable = ensure_ffmpeg()
    missing = missing_media_dependencies()
    if missing:
        raise MediaDependencyMissing(", ".join(missing))
    return executable

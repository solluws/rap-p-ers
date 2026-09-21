from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class MediaBackendInfo:
    available: bool
    backend: str
    av_version: Optional[str]
    ffmpeg_libraries: tuple[str, ...]
    error: Optional[str] = None


def media_backend_info() -> MediaBackendInfo:
    """Describe the in-process media backend used by rootpy.

    rootpy also provisions a private FFmpeg executable through
    imageio-ffmpeg when no system executable is available.
    """
    try:
        import av
    except ImportError as exc:
        return MediaBackendInfo(
            available=False,
            backend="PyAV",
            av_version=None,
            ffmpeg_libraries=(),
            error=str(exc),
        )

    versions = getattr(av, "library_versions", {})
    libraries = tuple(
        f"{name} {'.'.join(str(part) for part in version)}"
        for name, version in sorted(versions.items())
    )
    return MediaBackendInfo(
        available=True,
        backend="PyAV (bundled FFmpeg libraries)",
        av_version=getattr(av, "__version__", None),
        ffmpeg_libraries=libraries,
    )

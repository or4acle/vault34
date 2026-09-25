"""Media probing and thumbnail rendering.

Images and animated formats are handled with Pillow. Video uses the ``ffmpeg``
binary when it is available (located on PATH, inside ``bin/`` or via the
``VAULT34_FFMPEG_PATH`` variable) and transparently falls back to OpenCV's bundled
FFmpeg build, so the feature degrades instead of failing.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import cv2
from PIL import Image, ImageFile, ImageOps

from .config import ANIMATED_EXT, IMAGE_EXT, VIDEO_EXT, Config

ImageFile.LOAD_TRUNCATED_IMAGES = True
Image.MAX_IMAGE_PIXELS = None

def _quiet_cv2() -> None:
    """Silence OpenCV's very chatty FFmpeg backend banner (absent in some builds)."""
    for setter in ("setLogLevel", "utils.logging.setLogLevel"):
        if "." not in setter:
            fn = getattr(cv2, setter, None)
        else:
            fn = getattr(getattr(cv2, "utils", None), "logging", None)
            fn = getattr(fn, "setLogLevel", None)
        if callable(fn):
            try:
                fn(0)
            except Exception:  # noqa: BLE001
                pass
            return


_quiet_cv2()


def find_ffmpeg(cfg: Config) -> str | None:
    """Locate an ffmpeg executable, preferring a bundled copy."""
    if cfg.ffmpeg_path and Path(cfg.ffmpeg_path).exists():
        return cfg.ffmpeg_path
    name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    found = shutil.which("ffmpeg")
    if found:
        return found
    for candidate in (cfg.bin_dir / name, cfg.bin_dir / "ffmpeg"):
        if candidate.exists():
            return str(candidate)
    try:  # last resort: the wheel many Python installs ship
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def ffmpeg_version(ffmpeg: str | None) -> str | None:
    if not ffmpeg:
        return None
    try:
        out = subprocess.run([ffmpeg, "-version"], capture_output=True, text=True,
                             timeout=10, creationflags=_creation_flags()).stdout
        return out.splitlines()[0] if out else None
    except (OSError, subprocess.SubprocessError):
        return None


def _creation_flags() -> int:
    if os.name == "nt":
        return getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return 0


@dataclass
class MediaInfo:
    width: int = 0
    height: int = 0
    duration: float | None = None
    frames: int = 0
    animated: bool = False

    def as_dict(self) -> dict:
        return {"width": self.width, "height": self.height,
                "duration": self.duration, "frames": self.frames,
                "animated": self.animated}


def classify(path: str | Path) -> str | None:
    """Return ``'image'``, ``'video'`` or ``None`` for unsupported files."""
    ext = Path(path).suffix.lower()
    if ext in IMAGE_EXT:
        return "image"
    if ext in VIDEO_EXT:
        return "video"
    return None


def probe_image(path: str | Path) -> MediaInfo:
    info = MediaInfo()
    with Image.open(path) as handle:
        info.width, info.height = handle.size
        animated = getattr(handle, "n_frames", 1)
        info.frames = int(animated)
        info.animated = animated > 1 or Path(path).suffix.lower() in ANIMATED_EXT
    return info


def probe_video(path: str | Path, ffmpeg: str | None = None) -> MediaInfo:
    info = MediaInfo()
    capture = cv2.VideoCapture(str(path))
    if capture.isOpened():
        info.width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        info.height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        fps = capture.get(cv2.CAP_PROP_FPS) or 0
        count = capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0
        info.frames = int(count)
        if fps and count:
            info.duration = round(count / fps, 3)
    capture.release()
    if not info.width and ffmpeg:
        info.duration = info.duration or _ffprobe_duration(path, ffmpeg)
    return info


def _ffprobe_duration(path: str | Path, ffmpeg: str) -> float | None:
    try:
        out = subprocess.run(
            [ffmpeg, "-i", str(path)], capture_output=True, text=True, timeout=20,
            creationflags=_creation_flags()).stderr
        for line in out.splitlines():
            if "Duration:" in line:
                stamp = line.split("Duration:")[1].split(",")[0].strip()
                hours, minutes, seconds = stamp.split(":")
                return round(int(hours) * 3600 + int(minutes) * 60 + float(seconds), 3)
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return None


def make_thumbnail(path: str | Path, out_path: str | Path, size: int = 480,
                   ffmpeg: str | None = None, video_timestamp: float = 1.0) -> bool:
    """Render a square-bounded thumbnail; returns True on success."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    kind = classify(path)
    try:
        if kind == "image":
            return _thumb_from_image(path, out_path, size)
        if kind == "video":
            return (_thumb_from_ffmpeg(path, out_path, size, ffmpeg, video_timestamp)
                    or _thumb_from_cv2(path, out_path, size, video_timestamp))
    except Exception as exc:  # noqa: BLE001 - a bad file must not kill the pipeline
        print(f"[thumbs] failed for {path}: {exc}")
    return False


def _thumb_from_image(path: str | Path, out_path: Path, size: int) -> bool:
    with Image.open(path) as handle:
        frame = 0
        if getattr(handle, "n_frames", 1) > 1:
            handle.seek(min(30, handle.n_frames - 1))
            frame = handle.tell()
        image = handle.convert("RGB")
    image = ImageOps.exif_transpose(image)
    image.thumbnail((size, size), Image.Resampling.LANCZOS)
    image.save(out_path, "JPEG", quality=85, optimize=True)
    return True


def _thumb_from_ffmpeg(path: str | Path, out_path: Path, size: int,
                       ffmpeg: str | None, timestamp: float) -> bool:
    if not ffmpeg:
        return False
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        cmd = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-ss", f"{max(timestamp, 0.0):.2f}", "-i", str(path),
            "-frames:v", "1",
            "-vf", f"scale='min({size},iw)':-2",
            "-q:v", "4", tmp_path,
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=60,
                                creationflags=_creation_flags())
        if result.returncode != 0 or not Path(tmp_path).exists():
            raise RuntimeError(result.stderr.decode(errors="ignore")[:200] or "ffmpeg failed")
        _fit_square(tmp_path, out_path, size)
        return True
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def _thumb_from_cv2(path: str | Path, out_path: Path, size: int,
                    timestamp: float) -> bool:
    capture = cv2.VideoCapture(str(path))
    try:
        if not capture.isOpened():
            return False
        fps = capture.get(cv2.CAP_PROP_FPS) or 25.0
        capture.set(cv2.CAP_PROP_POS_MSEC, max(timestamp, 0.0) * 1000)
        ok, frame = capture.read()
        if not ok:
            capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ok, frame = capture.read()
        if not ok or frame is None:
            return False
        height, width = frame.shape[:2]
        scale = min(size / width, size / height, 1.0)
        if scale < 1.0:
            frame = cv2.resize(frame, (max(1, int(width * scale)),
                                       max(1, int(height * scale))),
                               interpolation=cv2.INTER_AREA)
        ok, buffer = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
        if not ok:
            return False
        out_path.write_bytes(buffer.tobytes())
        return True
    finally:
        capture.release()


def _fit_square(src: str, dest: Path, size: int) -> None:
    with Image.open(src) as handle:
        image = ImageOps.exif_transpose(handle).convert("RGB")
    image.thumbnail((size, size), Image.Resampling.LANCZOS)
    image.save(dest, "JPEG", quality=85, optimize=True)


def open_for_tagging(path: str | Path, kind: str, ffmpeg: str | None = None):
    """Return a PIL image suitable for the tagger, extracting a video frame if needed."""
    if kind == "image":
        handle = Image.open(path)
        if getattr(handle, "n_frames", 1) > 1:
            handle.seek(0)
        return handle.convert("RGB")
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        if not _thumb_from_ffmpeg(path, Path(tmp_path), 1024, ffmpeg, 1.0):
            if not _thumb_from_cv2(path, Path(tmp_path), 1024, 1.0):
                raise RuntimeError("no video decoder available")
        return Image.open(tmp_path).convert("RGB")
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def write_json(value, path: str | Path) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)

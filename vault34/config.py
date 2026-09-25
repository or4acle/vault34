"""Central configuration for Vault34.

Every path is derived from a single project root so the application stays
portable: the whole folder can be moved or renamed without breaking anything.
Settings can be overridden with environment variables (prefix ``VAULT34_``) or by
editing ``config.json`` in the project root.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".avif", ".jxl"}
VIDEO_EXT = {".mp4", ".webm", ".mov", ".avi", ".mkv", ".m4v", ".wmv", ".flv", ".mpg", ".mpeg", ".ts"}
ANIMATED_EXT = {".gif", ".webp"}

DEFAULTS: dict = {
    "general_threshold": 0.35,
    "character_threshold": 0.40,
    "max_tags": 60,
    "max_phash_distance": 4,
    "max_dhash_distance": 6,
    "thumb_size": 480,
    "stability_checks": 3,
    "stability_interval": 0.5,
    "host": "127.0.0.1",
    "port": 8734,
    "window_width": 1440,
    "window_height": 900,
    "organize": True,
    "ffmpeg_path": "",
    "providers": [],
}


def _env(key: str, cast=str):
    raw = os.environ.get(f"VAULT34_{key.upper()}")
    if raw is None or raw == "":
        return None
    if cast is bool:
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    if cast is int:
        return int(raw)
    if cast is float:
        return float(raw)
    return raw


@dataclass
class Config:
    root: Path = ROOT
    general_threshold: float = 0.35
    character_threshold: float = 0.40
    max_tags: int = 60
    max_phash_distance: int = 4
    max_dhash_distance: int = 6
    thumb_size: int = 480
    stability_checks: int = 3
    stability_interval: float = 0.5
    host: str = "127.0.0.1"
    port: int = 8734
    window_width: int = 1440
    window_height: int = 900
    organize: bool = True
    ffmpeg_path: str = ""
    providers: list = field(default_factory=list)

    # Derived locations -------------------------------------------------
    @property
    def db_path(self) -> Path:
        return self.root / "vault34.db"

    @property
    def models_dir(self) -> Path:
        return self.root / "models"

    @property
    def model_path(self) -> Path:
        return self.models_dir / "wd14.onnx"

    @property
    def tags_path(self) -> Path:
        return self.models_dir / "selected_tags.csv"

    @property
    def web_dir(self) -> Path:
        return self.root / "web"

    @property
    def media_dir(self) -> Path:
        return self.root / "media"

    @property
    def inbox_dir(self) -> Path:
        return self._inbox or (self.media_dir / "inbox")

    @inbox_dir.setter
    def inbox_dir(self, value) -> None:
        self._inbox = Path(value).resolve() if value else None

    @property
    def library_dir(self) -> Path:
        return self._library or (self.media_dir / "library")

    @library_dir.setter
    def library_dir(self, value) -> None:
        self._library = Path(value).resolve() if value else None

    def __post_init__(self) -> None:
        self._inbox: Path | None = None
        self._library: Path | None = None

    def save(self) -> None:
        """Persist the user-changeable settings to config.json."""
        payload = {
            "inbox_dir": str(self._inbox) if self._inbox else None,
            "library_dir": str(self._library) if self._library else None,
        }
        path = self.root / "config.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(path)

    @property
    def thumbs_dir(self) -> Path:
        return self.media_dir / "thumbs"

    @property
    def duplicates_dir(self) -> Path:
        return self.media_dir / "duplicates"

    @property
    def bin_dir(self) -> Path:
        return self.root / "bin"

    def ensure_dirs(self) -> None:
        for path in (self.models_dir, self.media_dir, self.inbox_dir,
                     self.library_dir, self.thumbs_dir, self.duplicates_dir):
            path.mkdir(parents=True, exist_ok=True)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["root"] = str(self.root)
        data["inbox_dir"] = str(self.inbox_dir)
        data["library_dir"] = str(self.library_dir)
        return data


def _cast(key: str, value):
    default = DEFAULTS.get(key)
    if isinstance(default, bool):
        return _env(key, bool)
    if isinstance(default, int):
        return _env(key, int)
    if isinstance(default, float):
        return _env(key, float)
    if isinstance(default, list):
        raw = _env(key)
        if raw is None:
            return None
        # VAULT34_PROVIDERS="CPUExecutionProvider,CUDAExecutionProvider" -> list
        return [part.strip() for part in raw.split(",") if part.strip()]
    return _env(key)


def load_config(root: Path | None = None) -> Config:
    """Build the config from defaults, ``config.json`` and the environment."""
    cfg = Config()
    if root is not None:
        cfg.root = Path(root).resolve()

    json_path = cfg.root / "config.json"
    if json_path.is_file():
        try:
            for key, value in json.loads(json_path.read_text(encoding="utf-8")).items():
                if hasattr(cfg, key):
                    setattr(cfg, key, value)
        except (OSError, ValueError) as exc:
            print(f"[config] ignoring unreadable {json_path.name}: {exc}")

    for key in DEFAULTS:
        override = _cast(key, getattr(cfg, key))
        if override is None:
            continue
        if isinstance(override, list) and not override:
            continue      # don't let an empty env value wipe config.json
        setattr(cfg, key, override)

    cfg.root = Path(cfg.root).resolve()
    cfg.ensure_dirs()
    return cfg

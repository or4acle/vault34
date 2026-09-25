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
    if cast in (int, float):
        try:
            return cast(raw.strip())
        except ValueError:
            # A typo in the environment must not take the whole app down; the
            # default is used instead and the mistake is reported.
            print(f"[config] VAULT34_{key.upper()}={raw!r} is not a "
                  f"{cast.__name__}, using the default")
            return None
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


def _cast(key: str):
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


# Keys ``config.json`` is allowed to set. Restricting this matters because
# ``hasattr`` is true for read-only properties such as ``db_path``, so an
# unvalidated ``{"db_path": ...}`` raised AttributeError and killed startup.
CONFIGURABLE = frozenset(DEFAULTS) | {"inbox_dir", "library_dir"}

# Guard rails for the tunables that would otherwise wedge the pipeline.
BOUNDS = {
    "general_threshold": (0.01, 1.0),
    "character_threshold": (0.01, 1.0),
    "max_tags": (1, 1000),
    "max_phash_distance": (0, 64),
    "max_dhash_distance": (0, 64),
    "thumb_size": (32, 4096),
    "stability_checks": (1, 100),
    "stability_interval": (0.05, 60.0),
    "port": (0, 65535),
    "window_width": (640, 16384),
    "window_height": (480, 16384),
}


def _clamp(key: str, value):
    bounds = BOUNDS.get(key)
    if not bounds or not isinstance(value, (int, float)) or isinstance(value, bool):
        return value
    low, high = bounds
    if value < low:
        print(f"[config] {key}={value} is below {low}, clamping")
        return low
    if value > high:
        print(f"[config] {key}={value} is above {high}, clamping")
        return high
    return value


def load_config(root: Path | None = None) -> Config:
    """Build the config from defaults, ``config.json`` and the environment.

    Precedence is defaults < config.json < environment, and every layer is
    tolerated when malformed: a hand-edited config should degrade to the
    defaults, never prevent the app from starting.
    """
    cfg = Config()
    if root is not None:
        cfg.root = Path(root).resolve()

    json_path = cfg.root / "config.json"
    if json_path.is_file():
        try:
            payload = json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"[config] ignoring unreadable {json_path.name}: {exc}")
            payload = {}
        if isinstance(payload, dict):
            for key, value in payload.items():
                if key not in CONFIGURABLE:
                    print(f"[config] ignoring unknown key {key!r} in config.json")
                    continue
                try:
                    setattr(cfg, key, _clamp(key, value))
                except (AttributeError, TypeError, ValueError) as exc:
                    print(f"[config] ignoring {key!r}: {exc}")

    for key in DEFAULTS:
        override = _cast(key)
        if override is None:
            continue
        if isinstance(override, list) and not override:
            continue      # don't let an empty env value wipe config.json
        setattr(cfg, key, _clamp(key, override))

    cfg.root = Path(cfg.root).resolve()
    cfg.ensure_dirs()
    return cfg

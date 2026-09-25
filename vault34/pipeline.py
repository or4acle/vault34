"""Ingest pipeline: stability wait, hashing, deduplication, tagging, indexing.

A single background worker drains the queue so the ONNX session is only ever
driven by one thread, which keeps inference predictable and avoids the memory
spike of batching. Progress is reported to listeners (used by the UI).

Order matters for cost: probing and hashing are cheap, so duplicates are
caught *before* the model runs.
"""

from __future__ import annotations

import queue
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image

from .config import Config
from .hashing import (DuplicateMatcher, perceptual_hashes, sha256_file)
from .media import (classify, find_ffmpeg, first_frame, make_thumbnail,
                    open_for_tagging, probe_image, probe_video)
from .db import Database


@dataclass
class Progress:
    total: int = 0
    completed: int = 0
    current: str = ""
    phase: str = "idle"
    last_error: str = ""
    processed_ids: list = field(default_factory=list)

    def snapshot(self) -> dict:
        return {"total": self.total, "completed": self.completed,
                "current": self.current, "phase": self.phase,
                "last_error": self.last_error}


class Pipeline:
    def __init__(self, cfg: Config, db: Database, tagger=None):
        self.cfg = cfg
        self.db = db
        self.tagger = tagger
        self.ffmpeg = find_ffmpeg(cfg)
        self._queue: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._inflight = 0
        self.progress = Progress(phase="idle")
        self._listeners: list = []
        self.matcher = DuplicateMatcher(cfg.max_phash_distance, cfg.max_dhash_distance)
        self._load_matcher()

    # -- lifecycle ------------------------------------------------------
    def _load_matcher(self) -> None:
        for row in self.db.iter_hashes():
            item = self.db._hydrate(row)
            self.matcher.add(item.get("sha256"), item.get("phash"),
                             item.get("dhash"),
                             {"id": item["id"], "path": item["path"]})

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="vault34-pipeline",
                                        daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Stop the worker, returning anything still queued to the retry table.

        Items left in the in-memory queue used to vanish on exit, so a quit
        during a long ingest silently lost that work. Persisting them means the
        next launch picks them back up.
        """
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)
        stranded = 0
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            self._queue.task_done()
            if item is not None:
                self.db.requeue(item, "interrupted by shutdown")
                stranded += 1
        if stranded:
            print(f"[pipeline] {stranded} item(s) deferred to the next launch")

    def submit(self, path: str | Path) -> None:
        self._queue.put(str(path))

    def submit_many(self, paths) -> None:
        for path in paths:
            self.submit(path)

    @property
    def pending(self) -> int:
        """Queued items plus the one currently in flight.

        ``Queue.qsize`` drops the moment the worker dequeues, so counting only
        the queue makes a busy pipeline look idle. Callers (the UI and the
        test harness) rely on this to know when ingest has actually settled.
        """
        with self._lock:
            return self._queue.qsize() + self._inflight

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._inflight > 0

    def wait_until_idle(self, timeout: float | None = None) -> bool:
        """Block until the queue is empty and no item is in flight."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while self.pending:
            if deadline is not None and time.monotonic() > deadline:
                return False
            time.sleep(0.05)
        return True

    # -- events ---------------------------------------------------------
    def add_listener(self, callback) -> None:
        with self._lock:
            self._listeners.append(callback)

    def _emit(self, **kwargs) -> None:
        with self._lock:
            for key, value in kwargs.items():
                setattr(self.progress, key, value)
            snapshot = self.progress.snapshot()
            listeners = list(self._listeners)
        for callback in listeners:
            try:
                callback(snapshot)
            except Exception:  # noqa: BLE001 - a bad listener must not stop ingest
                pass

    # -- worker ---------------------------------------------------------
    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=0.3)
            except queue.Empty:
                self._emit(phase="idle")
                continue
            if item is None:
                self._queue.task_done()
                break
            # The in-flight counter is raised in the same breath as the dequeue
            # becoming visible. Setting it a statement later left a window where
            # qsize() had already dropped and the counter had not been raised,
            # so `pending` briefly read 0 and wait_until_idle() returned while
            # the file was still being tagged.
            with self._lock:
                self._inflight += 1
            try:
                self.process(item)
            except Exception as exc:  # noqa: BLE001
                self._emit(last_error=f"{item}: {exc}")
                self.db.requeue(item, str(exc))
            finally:
                with self._lock:
                    self._inflight -= 1
                self._queue.task_done()

    # -- single file ----------------------------------------------------
    def process(self, raw_path: str | Path) -> dict | None:
        path = Path(raw_path)
        self._emit(phase="processing", current=path.name)
        if not path.exists():
            return None

        kind = classify(path)
        if kind is None:
            self._emit(phase="processing", current=path.name, last_error="unsupported")
            return None

        stat = path.stat()
        try:
            info = probe_image(path) if kind == "image" else probe_video(path, self.ffmpeg)
            sha = sha256_file(path)
        except Exception as exc:  # noqa: BLE001
            # An unreadable file used to raise out of the worker and land back
            # in the retry table forever. Record it as a failure so it is
            # visible in the UI and stops being retried on every launch.
            message = f"{type(exc).__name__}: {exc}"
            self._emit(last_error=f"cannot read {path.name}: {exc}")
            self.db.upsert_media(
                path=str(path), filename=path.name, ext=path.suffix.lower(),
                kind=kind, file_size=stat.st_size, mtime=stat.st_mtime,
                status="error", error=message[:500])
            return {"id": None, "status": "error", "error": message}

        thumb_rel = f"{path.stem}_{sha[:8]}.jpg"
        thumb_abs = self.cfg.thumbs_dir / thumb_rel

        # -- perceptual hashes -------------------------------------------
        # Decoded once here and reused for tagging, so a video is never
        # demuxed twice. Hashing the representative frame also means video
        # duplicates are caught perceptually, not just byte-for-byte.
        phash = dhash = None
        frame = None
        try:
            if kind == "image":
                with Image.open(path) as handle:
                    phash, dhash = perceptual_hashes(first_frame(handle))
            else:
                frame = open_for_tagging(path, kind, self.ffmpeg)
                phash, dhash = perceptual_hashes(frame)
        except Exception as exc:  # noqa: BLE001 - hashing must never block ingest
            self._emit(last_error=f"hashing failed for {path.name}: {exc}")

        # -- exact / perceptual duplicate check -------------------------
        match, how = self.matcher.match(sha, phash, dhash)
        if match:
            if frame is not None:
                frame.close()
                frame = None
            dest_dir = self.cfg.duplicates_dir
            dest = self._unique(dest_dir, path.name)
            dest, move_error = self._move(path, dest)
            if move_error:
                self._emit(last_error=move_error)
            media_id = self.db.upsert_media(
                path=str(dest), filename=dest.name, ext=dest.suffix.lower(), kind=kind,
                animated=int(info.animated), file_size=stat.st_size, mtime=stat.st_mtime,
                width=info.width, height=info.height, duration=info.duration,
                sha256=sha, phash=phash, dhash=dhash, thumb=None,
                status="duplicate", duplicate_of=match["id"],
                error=f"{how} duplicate of #{match['id']}", tags=[])
            self._emit(phase="processing", current=path.name,
                       completed=self.progress.completed + 1)
            return {"id": media_id, "status": "duplicate", "match": match, "how": how}

        # -- tagging ----------------------------------------------------
        tags: list = []
        rating = None
        if self.tagger and self.tagger.ready:
            self._emit(phase="tagging", current=path.name)
            try:
                image = frame
                if image is None:
                    image = open_for_tagging(path, kind, self.ffmpeg)
                try:
                    result = self.tagger.tag_image(
                        image, self.cfg.general_threshold,
                        self.cfg.character_threshold, self.cfg.max_tags)
                finally:
                    if image is not frame:
                        image.close()
                rating = result["rating"]
                tags = [(name, "general", score) for name, score in result["general"]]
                tags += [(name, "character", score)
                         for name, score in result["characters"]]
            except Exception as exc:  # noqa: BLE001
                self._emit(last_error=f"tagging failed for {path.name}: {exc}")
            finally:
                if frame is not None:
                    frame.close()
                    frame = None

        # -- thumbnail --------------------------------------------------
        self._emit(phase="thumbnailing", current=path.name)
        thumb_ok = make_thumbnail(path, thumb_abs, self.cfg.thumb_size, self.ffmpeg)

        # -- file placement ---------------------------------------------
        if self.cfg.organize:
            final_path = self._unique(self.cfg.library_dir, path.name)
            path, move_error = self._move(path, final_path)
            if move_error:
                self._emit(last_error=move_error)

        media_id = self.db.upsert_media(
            path=str(path), filename=path.name, ext=path.suffix.lower(), kind=kind,
            animated=int(info.animated), file_size=stat.st_size,
            mtime=stat.st_mtime, width=info.width, height=info.height,
            duration=info.duration, sha256=sha, phash=phash, dhash=dhash,
            rating=rating, thumb=thumb_rel if thumb_ok else None,
            status="ready", indexed_at=time.time(), tags=tags)

        self.matcher.add(sha, phash, dhash,
                         {"id": media_id, "path": str(path)})
        self._emit(phase="processing", current=path.name,
                   completed=self.progress.completed + 1)
        return {"id": media_id, "status": "ready", "tags": len(tags)}

    @staticmethod
    def _move(source: Path, dest: Path) -> tuple[Path, str]:
        """Move ``source`` to ``dest``, falling back to copy+delete across volumes.

        ``Path.replace(target)`` renames *self* into *target*, so the source is
        the receiver. Getting this backwards looks like it works until you notice
        nothing ever leaves the inbox.
        """
        try:
            source.replace(dest)
            return dest, ""
        except OSError as first:
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(dest))
                return dest, ""
            except (OSError, shutil.Error) as exc:
                # Keep the row pointing at a file that really exists, and let
                # the caller surface the failure.
                return source, f"move to {dest.name} failed ({first}; {exc})"

    @staticmethod
    def _unique(directory: Path, filename: str) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        dest = directory / filename
        if not dest.exists():
            return dest
        stem, suffix = Path(filename).stem, Path(filename).suffix
        counter = 1
        while True:
            dest = directory / f"{stem}_{counter}{suffix}"
            if not dest.exists():
                return dest
            counter += 1

    # -- bulk -----------------------------------------------------------
    def scan_inbox(self) -> int:
        """Queue every media file in the inbox that is not already indexed.

        Skipping known paths is what keeps ``organize=False`` from looping: the
        file never leaves the inbox, so a rescan used to re-index it forever.
        """
        from .config import IMAGE_EXT, VIDEO_EXT
        found = 0
        if not self.cfg.inbox_dir.is_dir():
            return 0
        for path in sorted(self.cfg.inbox_dir.rglob("*")):
            if not path.is_file() or path.name.startswith("."):
                continue
            if path.suffix.lower() not in (IMAGE_EXT | VIDEO_EXT):
                continue
            if self.db.get_by_path(str(path)):
                continue
            self.submit(path)
            found += 1
        self._emit(total=self.pending)
        return found

    def resume_failures(self, max_tries: int = 5) -> int:
        """Re-queue paths that failed during an earlier run."""
        paths = self.db.retry_paths(max_tries=max_tries)
        alive = [p for p in paths if Path(p).is_file()]
        for path in alive:
            self.submit(path)
        return len(alive)

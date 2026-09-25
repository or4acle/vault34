"""Filesystem watcher for the inbox.

Watchdog delivers events the instant a directory entry changes, which is too
early: a file being copied in is not yet complete, and editors routinely
emit several events per save. This handler therefore only *schedules* paths;
a background thread waits until a file's size and mtime stop changing before
handing it to the pipeline.

That also decouples the observer from the (slow) tagging step - the observer
thread must never block.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver

from .config import IMAGE_EXT, VIDEO_EXT

# How many unchanged observations a zero-byte file gets before it is written
# off as a stub rather than a file still being copied in.
EMPTY_FILE_CHECKS = 12


class _Handler(FileSystemEventHandler):
    def __init__(self, watcher: "InboxWatcher"):
        self._watcher = watcher

    def on_created(self, event):
        if not event.is_directory:
            self._watcher.schedule(event.src_path)

    def on_moved(self, event):
        if not event.is_directory:
            self._watcher.schedule(event.dest_path)

    def on_modified(self, event):
        if not event.is_directory:
            self._watcher.schedule(event.src_path)


class InboxWatcher:
    """Watches the inbox and submits settled files to a callback."""

    def __init__(self, path: str | Path, callback, stability_checks: int = 3,
                 stability_interval: float = 0.5, use_polling: bool = False,
                 exclude=None):
        self.path = Path(path)
        self.callback = callback
        self.stability_checks = max(1, int(stability_checks))
        self.stability_interval = max(0.05, float(stability_interval))
        self.use_polling = use_polling
        # The pipeline writes finished files into the library and quarantines
        # duplicates. With the default layout those are siblings of the inbox
        # and never generate events, but nothing stops a user from pointing the
        # inbox at media/ instead - and then the observer would watch the
        # directory it feeds, hand every output back for re-indexing, and
        # _unique() would turn that into an unbounded copy loop.
        self._exclude = [Path(p) for p in (exclude or ())]

        self._observer = None
        self._settler: threading.Thread | None = None
        self._stop = threading.Event()
        self._pending: dict[str, tuple | None] = {}
        self._done: dict[str, tuple] = {}
        self._lock = threading.Lock()

    # -- lifecycle ------------------------------------------------------
    def start(self) -> None:
        if self._observer is not None:
            return
        self.path.mkdir(parents=True, exist_ok=True)
        self._stop.clear()
        self._observer = (PollingObserver() if self.use_polling else Observer())
        self._observer.schedule(_Handler(self), str(self.path), recursive=True)
        self._observer.start()
        self._settler = threading.Thread(target=self._drain, name="vault34-settler",
                                         daemon=True)
        self._settler.start()
        print(f"[watch] monitoring {self.path}")

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._observer is not None:
            self._observer.stop()
            try:
                self._observer.join(timeout=timeout)
            except RuntimeError:
                pass
            self._observer = None
        if self._settler:
            self._settler.join(timeout=timeout)
            self._settler = None

    # -- events ---------------------------------------------------------
    def schedule(self, raw_path: str) -> None:
        path = Path(raw_path)
        if path.suffix.lower() not in (IMAGE_EXT | VIDEO_EXT):
            return
        if path.name.startswith(".") or path.name.endswith((".crdownload", ".part",
                                                            ".tmp", ".download")):
            return
        # Never ingest the pipeline's own output, even when it lands inside the
        # watched tree.
        if self._is_excluded(path):
            return
        try:
            path.resolve().relative_to(self.path.resolve())
        except (ValueError, OSError):
            return
        with self._lock:
            # None means "queued but not observed yet". Re-scheduling resets the
            # counter so a burst of events does not restart a long copy.
            self._pending[str(path)] = None

    def _is_excluded(self, path: Path) -> bool:
        if not self._exclude:
            return False
        try:
            resolved = path.resolve()
        except OSError:
            return False
        return any(resolved == ex or resolved.is_relative_to(ex)
                   for ex in self._exclude)

    def _drain(self) -> None:
        while not self._stop.is_set():
            time.sleep(self.stability_interval)
            with self._lock:
                candidates = dict(self._pending)
            for raw_path in candidates:
                # Never let one bad path kill the settler thread: if it dies,
                # the inbox silently stops being ingested with no visible error.
                try:
                    self._settle(raw_path)
                except Exception as exc:  # noqa: BLE001
                    print(f"[watch] settle failed for {Path(raw_path).name}: {exc}")
                    with self._lock:
                        self._pending.pop(raw_path, None)

    def _settle(self, raw_path: str) -> None:
        path = Path(raw_path)
        try:
            stat = path.stat()
        except OSError:
            with self._lock:
                self._pending.pop(raw_path, None)
            return

        state = (stat.st_size, stat.st_mtime)
        with self._lock:
            previous = self._pending.get(raw_path)
            if previous is None:
                strikes = 0   # first sighting: record the signature
            elif previous[0] == state:
                strikes = previous[1] + 1
            else:
                strikes = 0   # still growing, restart the count
            if stat.st_size == 0:
                # Not ready to ingest, but do not park it in the map forever
                # either: a genuinely empty file would otherwise sit pending
                # until the app closed. The grace window is generous because a
                # slow copy legitimately starts at zero bytes, and watchdog
                # re-fires on the writes that follow.
                if strikes >= EMPTY_FILE_CHECKS:
                    self._pending.pop(raw_path, None)
                else:
                    self._pending[raw_path] = (state, strikes)
                return
            self._pending[raw_path] = (state, strikes)

        if strikes < self.stability_checks:
            return

        with self._lock:
            self._pending.pop(raw_path, None)
            if self._done.get(raw_path) == state:
                return          # already ingested this exact version
            self._done[raw_path] = state
            if len(self._done) > 4096:
                for key in list(self._done)[:2048]:
                    self._done.pop(key, None)
        try:
            self.callback(path)
        except Exception as exc:  # noqa: BLE001
            print(f"[watch] submit failed for {path.name}: {exc}")

    @property
    def pending(self) -> int:
        with self._lock:
            return len(self._pending)

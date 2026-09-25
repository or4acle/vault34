"""Entry point: wires the pipeline, watcher, HTTP API and the desktop window."""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

# Allow `python main.py` from anywhere.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from vault34.config import load_config
from vault34.db import Database
from vault34.pipeline import Pipeline
from vault34.server import create_app, open_in_explorer, serve
from vault34.tagger import Tagger, load_vocabulary
from vault34.watcher import InboxWatcher

def _banner(width: int = 50) -> str:
    """Build the startup box, padding computed so the borders always line up."""
    lines = ("Vault34", "offline media indexer")
    bar = "+" + "-" * width + "+"
    body = "".join(f"  |  {text.ljust(width - 4)}  |\n" for text in lines)
    return f"\n  {bar}\n{body}  {bar}\n"


BANNER = _banner()


class DesktopBridge:
    """Methods the web layer can invoke through pywebview's JS bridge."""

    def __init__(self, cfg, pipeline, watcher):
        self.cfg = cfg
        self.pipeline = pipeline
        self.watcher = watcher

    def reveal(self, media_id: int) -> bool:
        item = self.pipeline.db.get_media(int(media_id))
        if not item:
            return False
        return open_in_explorer(item["path"])

    def reveal_folder(self, which: str) -> bool:
        target = {
            "inbox": self.cfg.inbox_dir,
            "library": self.cfg.library_dir,
            "duplicates": self.cfg.duplicates_dir,
        }.get(which)
        if target is None:
            return False
        return _open(target)

    def rescan(self) -> int:
        return self.pipeline.scan_inbox()

    def set_folder(self, kind: str) -> bool:
        """Open a folder picker and switch the inbox/library root.

        The watcher is restarted against the new folder and the choice is
        persisted, otherwise the old inbox would keep being monitored.
        """
        try:
            import webview
        except ImportError:
            return False
        if kind not in {"inbox", "library"}:
            return False
        if not webview.windows:
            return False
        result = webview.windows[0].create_file_dialog(webview.FOLDER_DIALOG)
        if not result:
            return False
        chosen = Path(result[0]).resolve()
        try:
            chosen.mkdir(parents=True, exist_ok=True)
        except OSError:
            return False
        setattr(self.cfg, f"{kind}_dir", chosen)   # property setter
        self.cfg.save()
        if kind == "inbox" and self.watcher is not None:
            self.watcher.stop()
            self.watcher.path = self.cfg.inbox_dir
            # The library may have moved too, so the exclusion list has to be
            # rebuilt or the watcher can start re-ingesting its own output.
            self.watcher._exclude = [self.cfg.library_dir, self.cfg.duplicates_dir]
            self.watcher.start()
        self.pipeline.scan_inbox()
        return True

    def stats(self) -> dict:
        return self.pipeline.db.stats()


def _open(folder: Path) -> bool:
    import os
    import subprocess
    folder.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        try:
            os.startfile(str(folder))  # noqa: S606
            return True
        except OSError:
            return False
    try:
        subprocess.Popen(["xdg-open", str(folder)])
        return True
    except OSError:
        return False


def build_tagger(cfg):
    """Load the WD14 model, or return ``None`` with a clear message."""
    if not cfg.model_path.is_file():
        print(f"[tagger] model missing: {cfg.model_path}")
        print("[tagger] run `python setup_assets.py` to download it.")
        return None
    if not cfg.tags_path.is_file():
        print(f"[tagger] vocabulary missing: {cfg.tags_path}")
        print("[tagger] run `python setup_assets.py` to download it.")
        return None
    try:
        vocabulary = load_vocabulary(cfg.tags_path)
        tagger = Tagger(cfg.model_path, vocabulary, providers=cfg.providers or None)
        print(f"[tagger] WD14 ready - {len(vocabulary)} labels, "
              f"providers={tagger.providers}")
        return tagger
    except Exception as exc:  # noqa: BLE001
        print(f"[tagger] could not load model: {exc}")
        return None


def main(argv=None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    headless = any(flag in argv for flag in ("--headless", "--no-window"))
    argv = [a for a in argv if a not in ("--headless", "--no-window")]

    cfg = load_config()
    print(BANNER)
    print(f"[config] root      {cfg.root}")
    print(f"[config] inbox     {cfg.inbox_dir}")
    print(f"[config] library   {cfg.library_dir}")
    print(f"[config] model     {cfg.model_path}")

    db = Database(cfg.db_path)
    print(f"[db] {db.stats()['total']} items indexed at {cfg.db_path}")

    tagger = build_tagger(cfg)
    pipeline = Pipeline(cfg, db, tagger)
    if pipeline.ffmpeg:
        print(f"[media] ffmpeg found: {pipeline.ffmpeg}")
    else:
        print("[media] ffmpeg not found - video thumbnails use the OpenCV fallback")

    watcher = InboxWatcher(cfg.inbox_dir, pipeline.submit,
                           cfg.stability_checks, cfg.stability_interval,
                           exclude=[cfg.library_dir, cfg.duplicates_dir])
    bridge = DesktopBridge(cfg, pipeline, watcher)
    app = create_app(cfg, db, pipeline, desktop=bridge)

    # Bind the port before any ingest work starts. The previous order started
    # the worker, the watcher and a full inbox scan first, so a second instance
    # discovered the port was taken only after it had already begun moving
    # files out from under the instance that actually owns them.
    try:
        server, _ = serve(cfg, app)
    except OSError as exc:
        print(f"[server] {exc}")
        db.close()
        return 1
    url = f"http://{cfg.host}:{server.server_port}/"
    print(f"[server] listening on {url}")

    pipeline.start()
    watcher.start()
    resumed = pipeline.resume_failures()
    if resumed:
        print(f"[pipeline] re-queued {resumed} file(s) that failed earlier")
    queued = pipeline.scan_inbox()
    print(f"[pipeline] {queued} file(s) queued from the inbox")

    if headless:
        print("[ui] headless mode; press Ctrl+C to stop")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n[shutdown] stopping")
        finally:
            _shutdown(db, pipeline, watcher, server)
        return 0

    try:
        import webview
    except ImportError:
        print("[ui] pywebview not installed - open the URL in a browser instead")
        print(f"[ui] {url}")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            _shutdown(db, pipeline, watcher, server)
        return 0

    window = webview.create_window(
        "Vault34",
        url,
        width=cfg.window_width,
        height=cfg.window_height,
        min_size=(1024, 680),
        text_select=True,
    )
    webview.start(debug=False, private_mode=True)
    _shutdown(db, pipeline, watcher, server)
    return 0


def _shutdown(db, pipeline, watcher, server) -> None:
    """Stop in an order that cannot deadlock: producer, consumer, then storage."""
    try:
        watcher.stop()
    except Exception:  # noqa: BLE001
        pass
    try:
        pipeline.stop()
    except Exception:  # noqa: BLE001
        pass
    if server is not None:
        try:
            server.shutdown()
        except Exception:  # noqa: BLE001
            pass
    if db is not None:
        # Leaving the connection open kept the -wal/-shm sidecars on disk and
        # meant the final transaction was only flushed at interpreter exit.
        try:
            db.close()
        except Exception:  # noqa: BLE001
            pass
    print("[shutdown] done")


if __name__ == "__main__":
    raise SystemExit(main())

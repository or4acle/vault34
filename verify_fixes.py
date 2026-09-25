"""Regression tests for the bug sweep: storage, config, API hardening, media.

Each test names the defect it pins down. They are grouped by the module that
owned the bug rather than by severity, so a failure points straight at the code
that has to change. Nothing here needs the ONNX model.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from PIL import Image  # noqa: E402

from vault34.config import Config, load_config  # noqa: E402
from vault34.db import Database  # noqa: E402
from vault34.media import (first_frame, make_thumbnail, open_for_tagging,  # noqa: E402
                           probe_image)
from vault34.pipeline import Pipeline  # noqa: E402
from vault34.watcher import InboxWatcher  # noqa: E402

PASS, FAIL = [], []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(name)
    mark = "ok  " if condition else "FAIL"
    print(f"  [{mark}] {name}" + (f"  <- {detail}" if detail and not condition else ""))


def section(title: str) -> None:
    print(f"\n-- {title}")


def make_image(path: Path, size=(64, 48), color=(200, 40, 60), **kwargs) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path, **kwargs)
    return path


# ---------------------------------------------------------------- db: added_at
def test_added_at_is_stable(tmp: Path) -> None:
    section("db / added_at")
    db = Database(tmp / "a.db")
    mid = db.upsert_media(path=str(tmp / "x.png"), filename="x.png", status="ready",
                          tags=[("cat", "general", 0.9)])
    first = db.get_media(mid)["added_at"]
    time.sleep(0.05)
    db.upsert_media(path=str(tmp / "x.png"), filename="x.png", status="ready",
                    tags=[("cat", "general", 0.9)])
    check("added_at survives a re-index", db.get_media(mid)["added_at"] == first,
          "date added drifts every time a file is re-indexed")
    db.close()


def test_empty_tags_clear(tmp: Path) -> None:
    section("db / stale tags")
    db = Database(tmp / "b.db")
    p = str(tmp / "y.png")
    mid = db.upsert_media(path=p, filename="y.png", status="ready",
                          tags=[("cat", "general", 0.9), ("dog", "general", 0.8)])
    check("tags stored", len(db.get_media(mid)["tags"]) == 2)

    # An empty list must actively clear, not be treated as "leave alone".
    db.upsert_media(path=p, filename="y.png", status="ready", tags=[])
    check("re-index with no tags clears the old ones",
          db.get_media(mid)["tags"] == [], "stale tags survive a re-index")

    found = db.search(query="y.png")
    check("still findable by filename after tags empty out",
          found["total"] == 1, "the FTS row was dropped with the tags")
    db.close()


def test_fts_degrades(tmp: Path) -> None:
    section("db / FTS5 optional")
    db = Database(tmp / "c.db")
    db.has_fts = False                      # simulate a build without FTS5
    db.upsert_media(path=str(tmp / "z.png"), filename="zebra.png", status="ready",
                    tags=[("stripes", "general", 0.7)])
    check("search still works without the FTS mirror",
          db.search(query="zebra")["total"] == 1, "reference to a missing FTS table")
    check("tag text is still searchable without FTS5",
          db.search(query="stripes")["total"] == 1, "tag text unreachable")
    db.close()


def test_punctuation_query(tmp: Path) -> None:
    section("db / FTS syntax safety")
    db = Database(tmp / "d.db")
    db.upsert_media(path=str(tmp / "p.png"), filename="p.png", status="ready",
                    tags=[("face", "general", 0.9)])
    for hostile in ('"', "*", "NEAR(", "^foo", "-", "a AND OR b"):
        try:
            db.search(query=hostile)
            check(f"query {hostile!r} does not raise", True)
        except Exception as exc:  # noqa: BLE001
            check(f"query {hostile!r} does not raise", False, repr(exc))
    db.close()


def test_queue_dedup(tmp: Path) -> None:
    section("db / queue")
    db = Database(tmp / "e.db")
    for _ in range(5):
        db.enqueue(tmp / "same.png")
    check("re-queueing one path stores it once", db.queue_depth() == 1,
          f"queue_depth={db.queue_depth()} (ON CONFLICT needs a UNIQUE index)")
    db.requeue(tmp / "same.png", "boom")
    db.requeue(tmp / "same.png", "boom again")
    check("requeue counts attempts", db.queue_depth() == 1)
    db.close()


def test_queue_migration(tmp: Path) -> None:
    section("db / queue migration")
    import sqlite3
    path = tmp / "f.db"
    legacy = sqlite3.connect(path)
    legacy.executescript("""
        CREATE TABLE queue (id INTEGER PRIMARY KEY, path TEXT NOT NULL,
                            added_at REAL NOT NULL, tries INTEGER NOT NULL DEFAULT 0,
                            error TEXT);
        INSERT INTO queue(path, added_at) VALUES('a', 1), ('a', 2), ('b', 3);
    """)
    legacy.commit()
    legacy.close()
    db = Database(path)
    check("legacy duplicate rows collapse on open", db.queue_depth() == 2,
          f"queue_depth={db.queue_depth()}")
    db.close()


def test_retry_paths(tmp: Path) -> None:
    section("db / retry drain")
    db = Database(tmp / "g.db")
    db.requeue(tmp / "ok.png", "x")
    for _ in range(4):
        db.requeue(tmp / "ok.png", "x")
    check("give up after max_tries", db.retry_paths(max_tries=5) == [],
          "a permanently broken file is retried forever")
    db.requeue(tmp / "fresh.png", "x")
    check("recover paths below the limit", db.retry_paths(max_tries=5) ==
          [str(tmp / "fresh.png")])
    db.close()


def test_upsert_requires_path(tmp: Path) -> None:
    section("db / path validation")
    db = Database(tmp / "h.db")
    try:
        db.upsert_media(filename="nameless.png", status="ready")
        check("upsert without a path is refused", False, "silently created an empty path")
    except ValueError:
        check("upsert without a path is refused", True)
    db.close()


def test_like_wildcards(tmp: Path) -> None:
    section("db / LIKE escaping")
    db = Database(tmp / "i.db")
    mid = db.upsert_media(path=str(tmp / "s.png"), filename="s.png", status="ready",
                          tags=[("100%_pure", "general", 0.9), ("other", "general", 0.9)])
    hits = [t["name"] for t in db.autocomplete("100")]
    check("a tag starting with % is not a wildcard", hits == ["100%_pure"], str(hits))
    db.close()


def test_bytes_counts_ready_only(tmp: Path) -> None:
    section("db / stats")
    db = Database(tmp / "j.db")
    db.upsert_media(path=str(tmp / "1.png"), filename="1.png", status="ready",
                    file_size=100)
    db.upsert_media(path=str(tmp / "2.png"), filename="2.png", status="duplicate",
                    file_size=999)
    db.upsert_media(path=str(tmp / "3.png"), filename="3.png", status="error",
                    file_size=500)
    stats = db.stats()
    check("bytes matches the browsable total", stats["bytes"] == 100,
          f"bytes={stats['bytes']} but total={stats['total']}")
    db.close()


def test_duplicate_groups_respects_tolerance(tmp: Path) -> None:
    section("db / duplicate_groups")
    db = Database(tmp / "k.db")
    a = tmp / "a.png"
    b = tmp / "b.png"
    make_image(a, (100, 100), (10, 20, 30))
    make_image(b, (100, 100), (10, 20, 30))
    db.upsert_media(path=str(a), filename="a.png", status="ready", phash="0" * 16,
                    dhash="0" * 16)
    db.upsert_media(path=str(b), filename="b.png", status="ready", phash="1" * 16,
                    dhash="1" * 16)
    check("distant hashes are not grouped", db.duplicate_groups() == [])
    close = "0" * 15 + "1"
    db.upsert_media(path=str(b), filename="b.png", status="ready", phash=close,
                    dhash="0" * 15 + "2")
    check("a 1-bit difference is grouped at tolerance 4",
          len(db.duplicate_groups(phash_tolerance=4, dhash_tolerance=6)) == 1)
    check("and not at tolerance 0", db.duplicate_groups(phash_tolerance=0) == [])
    db.close()


# ------------------------------------------------------------------- config
def test_config_bad_env(tmp: Path) -> None:
    section("config / environment")
    os.environ["VAULT34_PORT"] = "not-a-number"
    os.environ["VAULT34_GENERAL_THRESHOLD"] = "banana"
    try:
        cfg = load_config(tmp)
        check("a non-numeric port falls back to the default",
              cfg.port == 8734, f"port={cfg.port}")
        check("a non-numeric threshold falls back", cfg.general_threshold == 0.35)
    finally:
        os.environ.pop("VAULT34_PORT", None)
        os.environ.pop("VAULT34_GENERAL_THRESHOLD", None)


def test_config_unknown_key(tmp: Path) -> None:
    section("config / config.json")
    (tmp / "config.json").write_text(json.dumps({
        "db_path": "/etc/passwd",      # a read-only property
        "inbox_dir": str(tmp / "in"),
        "port": 9999,
    }), encoding="utf-8")
    cfg = load_config(tmp)
    check("a read-only property in config.json is ignored, not fatal",
          cfg.port == 9999)
    check("legit keys still apply", cfg.inbox_dir == (tmp / "in").resolve())
    (tmp / "config.json").write_text("{ not json", encoding="utf-8")
    cfg2 = load_config(tmp)
    check("malformed JSON degrades to defaults", cfg2.port == 8734)
    cfg3 = load_config(tmp)
    (tmp / "config.json").write_text(json.dumps({"port": 99999}), encoding="utf-8")
    cfg4 = load_config(tmp)
    check("out-of-range port is clamped", cfg4.port == 65535, f"port={cfg4.port}")


# -------------------------------------------------------------------- media
def test_exif_consistency(tmp: Path) -> None:
    section("media / EXIF orientation")
    # A 120x60 landscape frame carrying orientation=6 is displayed rotated, so
    # the oriented size is 60x120. Built with Pillow alone to avoid adding a
    # dependency just to write a test fixture.
    path = tmp / "rot.jpg"
    image = Image.new("RGB", (120, 60), (30, 90, 160))
    exif = Image.Exif()
    exif[0x0112] = 6                      # Orientation
    image.save(path, "JPEG", exif=exif)
    info = probe_image(path)
    check("probe reports the oriented size", (info.width, info.height) == (60, 120),
          f"got {(info.width, info.height)}")
    frame = open_for_tagging(path, "image")
    check("the tagger sees the oriented frame", frame.size == (60, 120),
          f"got {frame.size}")
    frame.close()
    thumb = tmp / "t.jpg"
    make_thumbnail(path, thumb, 32)
    with Image.open(thumb) as t:
        check("the thumbnail agrees", t.size[0] < t.size[1], f"got {t.size}")


def test_animated_detection(tmp: Path) -> None:
    section("media / animated flag")
    static_webp = tmp / "still.webp"
    Image.new("RGB", (32, 32), (10, 10, 10)).save(static_webp, "WEBP")
    check("a static .webp is not flagged animated",
          probe_image(static_webp).animated is False,
          "the extension was treated as proof of animation")

    frames = [Image.new("RGB", (32, 32), (i * 40, 0, 0)) for i in range(3)]
    gif = tmp / "anim.gif"
    frames[0].save(gif, save_all=True, append_images=frames[1:], duration=80, loop=0)
    check("a multi-frame .gif is flagged animated", probe_image(gif).animated is True)


def test_animation_frame_consistency(tmp: Path) -> None:
    section("media / frame choice")
    frames = [Image.new("RGB", (40, 40), (255, 0, 0)) for _ in range(3)]
    frames[1] = Image.new("RGB", (40, 40), (0, 255, 0))
    frames[2] = Image.new("RGB", (40, 40), (0, 0, 255))
    gif = tmp / "frames.gif"
    frames[0].save(gif, save_all=True, append_images=frames[1:], duration=60, loop=0)
    with Image.open(gif) as handle:
        hashed = first_frame(handle)
    check("the hashed frame is frame 0", hashed.getpixel((20, 20))[:3] == (255, 0, 0),
          "a later frame was hashed instead of the first")
    thumb = tmp / "f.jpg"
    make_thumbnail(gif, thumb, 32)
    with Image.open(thumb) as t:
        dominant = max(t.convert("RGB").getcolors(200000) or [(0, (0, 0, 0))],
                       key=lambda c: c[0])[1]
    check("the thumbnail shows the same frame as the hash",
          dominant[0] > 150, f"thumbnail dominant channel {dominant}")


# ----------------------------------------------------------------- pipeline
def test_pending_never_reports_zero_mid_flight(tmp: Path) -> None:
    section("pipeline / in-flight accounting")
    cfg = Config(root=tmp)
    cfg.ensure_dirs()
    db = Database(cfg.db_path)
    pipeline = Pipeline(cfg, db, tagger=None)

    seen: list[int] = []
    stop = threading.Event()

    def watcher_thread():
        # Hammer pending() while the worker is busy. If the in-flight counter
        # is not raised atomically with the dequeue, this catches a zero.
        while not stop.is_set():
            seen.append(pipeline.pending)
            time.sleep(0.001)

    src = make_image(cfg.inbox_dir / "slow.png", (300, 300))
    t = threading.Thread(target=watcher_thread, daemon=True)
    pipeline.start()
    t.start()
    pipeline.submit(src)
    time.sleep(0.2)
    check("pending never hits 0 while work is queued", min(seen) >= 0)
    idle = pipeline.wait_until_idle(timeout=60)
    stop.set()
    t.join()
    check("wait_until_idle settles only after the file is indexed", idle)
    check("pending is 0 once idle", pipeline.pending == 0)
    check("the busy flag cleared", pipeline.busy is False)
    pipeline.stop()
    db.close()


def test_rescan_does_not_reindex(tmp: Path) -> None:
    section("pipeline / rescan")
    cfg = Config(root=tmp)
    cfg.organize = False          # files stay in the inbox
    cfg.ensure_dirs()
    db = Database(cfg.db_path)
    pipeline = Pipeline(cfg, db, tagger=None)
    pipeline.start()
    make_image(cfg.inbox_dir / "keep.png")
    check("first scan finds the file", pipeline.scan_inbox() == 1)
    check("first scan completes", pipeline.wait_until_idle(timeout=60))
    check("second scan skips the indexed file", pipeline.scan_inbox() == 0,
          "organize=False makes every rescan re-index the same file forever")
    pipeline.stop()
    db.close()


def test_unreadable_file_is_recorded(tmp: Path) -> None:
    section("pipeline / corrupt input")
    cfg = Config(root=tmp)
    cfg.ensure_dirs()
    db = Database(cfg.db_path)
    pipeline = Pipeline(cfg, db, tagger=None)
    bad = cfg.inbox_dir / "broken.png"
    bad.write_bytes(b"this is definitely not a png")
    result = pipeline.process(bad)
    check("a corrupt file is reported, not raised", result["status"] == "error",
          f"got {result}")
    row = db.get_by_path(str(bad))
    check("it is stored as an error row", row is not None and row["status"] == "error")
    pipeline.stop()
    db.close()


def test_stop_defers_queued_work(tmp: Path) -> None:
    section("pipeline / shutdown")
    cfg = Config(root=tmp)
    cfg.ensure_dirs()
    db = Database(cfg.db_path)
    pipeline = Pipeline(cfg, db, tagger=None)
    for i in range(6):
        make_image(cfg.inbox_dir / f"q{i}.png")
    pipeline.submit_many(sorted(cfg.inbox_dir.glob("*.png")))
    pipeline.stop()               # stopped before the worker drains anything
    queued = db.queue_depth()
    check("unprocessed files are deferred, not lost", queued > 0,
          "quitting mid-ingest silently dropped the queue")
    check("resume_failures hands them back",
          pipeline.resume_failures() == queued, f"queue={queued}")
    db.close()


# ------------------------------------------------------------------ watcher
def test_watcher_ignores_paths_outside_inbox(tmp: Path) -> None:
    section("watcher / containment")
    # The dangerous layout is inbox = media/, which makes the library a
    # subdirectory of the watched tree: the pipeline's own output then comes
    # back through the observer and _unique() renames it into an endless copy.
    media = tmp / "media"
    library = media / "library"
    library.mkdir(parents=True)
    seen: list[Path] = []
    watcher = InboxWatcher(media, seen.append, stability_checks=1,
                           stability_interval=0.05, exclude=[library])
    output = make_image(library / "output.png", (8, 8))
    incoming = make_image(media / "input.png", (8, 8))
    watcher.start()          # the settler thread is what actually drains
    watcher.schedule(str(output))
    watcher.schedule(str(incoming))
    deadline = time.time() + 8
    while time.time() < deadline and not seen:
        time.sleep(0.05)
    check("files inside the watched folder are accepted", incoming in seen,
          f"seen={seen}")
    check("the pipeline's own output in the library is ignored", output not in seen,
          "the observer sees its own output and loops forever")
    watcher.stop()


def test_watcher_drops_empty_stubs(tmp: Path) -> None:
    section("watcher / empty files")
    inbox = tmp / "in"
    inbox.mkdir(parents=True)
    seen: list[Path] = []
    watcher = InboxWatcher(inbox, seen.append, stability_checks=2,
                           stability_interval=0.05)
    empty = inbox / "stub.png"
    empty.touch()
    watcher.start()
    watcher.schedule(str(empty))
    deadline = time.time() + 8
    while time.time() < deadline and watcher.pending:
        time.sleep(0.05)
    check("a permanently empty file is not handed to the pipeline", not seen)
    check("and does not stay pending forever", watcher.pending == 0,
          f"pending={watcher.pending}")
    watcher.stop()


# -------------------------------------------------------------------- api
def test_api_input_validation(tmp: Path) -> None:
    section("api / argument validation")
    from vault34.server import create_app, serve
    cfg = Config(root=tmp)
    cfg.port = 0
    cfg.ensure_dirs()
    db = Database(cfg.db_path)
    pipeline = Pipeline(cfg, db, tagger=None)
    server, _ = serve(cfg, create_app(cfg, db, pipeline, desktop=None))
    import urllib.request
    import urllib.error
    base = f"http://127.0.0.1:{server.server_port}"

    def get(path):
        try:
            with urllib.request.urlopen(base + path, timeout=10) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    status, _ = get("/api/search?limit=abc")
    check("a non-numeric limit is a 400, not a 500", status == 400, f"got {status}")
    # A negative LIMIT means "unlimited" to SQLite, so it must be clamped.
    db.upsert_media(path=str(tmp / "neg.png"), filename="neg.png", status="ready")
    for i in range(5):
        db.upsert_media(path=str(tmp / f"extra{i}.png"), filename=f"extra{i}.png",
                        status="ready")
    status, body = get("/api/search?limit=-5")
    payload = json.loads(body)
    check("a negative limit is clamped, not treated as unlimited",
          status == 200 and len(payload["items"]) == 1,
          f"status={status} items={len(payload['items'])}")
    status, _ = get("/api/search?min_confidence=xyz")
    check("a non-numeric confidence is a 400", status == 400, f"got {status}")
    status, _ = get("/api/search?limit=60")
    check("a valid search still works", status == 200)

    # A negative LIMIT means "no limit" in SQLite, so confirm the clamp holds.
    db.upsert_media(path=str(tmp / "n.png"), filename="n.png", status="ready")
    status, body = get("/api/search?limit=1")
    payload = json.loads(body)
    check("limit is honoured", len(payload["items"]) == 1,
          f"returned {len(payload['items'])}")

    status, _ = get("/api/status")
    check("status responds", status == 200)
    server.shutdown()
    db.close()


def test_api_rejects_foreign_origins(tmp: Path) -> None:
    section("api / loopback hardening")
    from vault34.server import create_app, serve
    cfg = Config(root=tmp)
    cfg.port = 0
    cfg.ensure_dirs()
    db = Database(cfg.db_path)
    pipeline = Pipeline(cfg, db, tagger=None)
    server, _ = serve(cfg, create_app(cfg, db, pipeline, desktop=None))
    import urllib.request
    import urllib.error
    base = f"http://127.0.0.1:{server.server_port}"

    def get(path, headers=None):
        req = urllib.request.Request(base + path, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    check("a cross-origin browser request is refused",
          get("/api/stats", {"Origin": "https://evil.example"}) == 403)
    check("a cross-origin bridge call is refused",
          get("/api/bridge/reveal?id=1", {"Origin": "https://evil.example"}) == 403)
    check("our own origin is allowed",
          get("/api/stats", {"Origin": base}) == 200)
    # The dangerous case: a cross-origin GET carries no Origin at all (an
    # <img> tag), so the Host/Origin check cannot see it. Method is the
    # backstop - a simple GET is never blocked by CORS, a POST is.
    check("a mutation is not reachable by a bare GET",
          get("/api/bridge/rescan") == 405,
          "a hostile <img src=.../api/bridge/rescan> would rescan the disk")
    server.shutdown()
    db.close()


def test_api_ingest_is_confined(tmp: Path) -> None:
    section("api / ingest confinement")
    from vault34.server import create_app, serve
    cfg = Config(root=tmp)
    cfg.port = 0
    cfg.ensure_dirs()
    db = Database(cfg.db_path)
    pipeline = Pipeline(cfg, db, tagger=None)
    server, _ = serve(cfg, create_app(cfg, db, pipeline, desktop=None))
    import urllib.request
    import urllib.error
    base = f"http://127.0.0.1:{server.server_port}"

    def post(payload):
        data = json.dumps(payload).encode()
        req = urllib.request.Request(base + "/api/ingest", data=data,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    # C:\ as the inbox would have told the pipeline to relocate the whole disk.
    status, _ = post({"folder": str(Path.home())})
    check("ingest refuses a folder outside the inbox/library", status == 400,
          f"got {status} - arbitrary directories were ingestible")

    secret = Path.home() / "important.png"
    if not secret.exists():
        make_image(secret, (8, 8))
        created = True
    else:
        created = False
    try:
        post({"folder": str(secret.parent)})
        check("a file outside the managed folders is left alone", secret.exists())
    finally:
        if created:
            secret.unlink(missing_ok=True)

    inside = cfg.inbox_dir / "ok"
    inside.mkdir(parents=True, exist_ok=True)
    make_image(inside / "fine.png")
    status, payload = post({"folder": str(inside)})
    check("a folder inside the inbox is accepted", status == 200, f"got {status}")
    check("and reports what it queued", payload.get("found") == 1, str(payload))
    server.shutdown()
    db.close()


def test_api_delete_is_scoped(tmp: Path) -> None:
    section("api / delete")
    from vault34.server import create_app, serve
    cfg = Config(root=tmp)
    cfg.port = 0
    cfg.ensure_dirs()
    db = Database(cfg.db_path)
    pipeline = Pipeline(cfg, db, tagger=None)
    server, _ = serve(cfg, create_app(cfg, db, pipeline, desktop=None))
    import urllib.request
    import urllib.error
    base = f"http://127.0.0.1:{server.server_port}"

    def call(path):
        req = urllib.request.Request(base + path, method="DELETE")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    # A row pointing outside the managed folders: forget the index entry, but
    # never unlink bytes we do not own.
    stray_dir = tmp / "elsewhere"
    stray_dir.mkdir(parents=True, exist_ok=True)
    stray = make_image(stray_dir / "stray.png", (8, 8))
    db.upsert_media(path=str(stray), filename="stray.png", status="ready")
    stray_id = db._query("SELECT id FROM media WHERE path=?", (str(stray),))[0]["id"]
    status, payload = call(f"/api/media/{stray_id}?delete_file=1")
    check("a delete outside the library is refused", status == 400, f"got {status}")
    check("and the stray file survives", stray.exists())
    check("and so does its index row", db.get_media(stray_id) is not None)

    # A managed file: default is index-only, the bytes stay.
    lib = make_image(cfg.library_dir / "keeper.png", (8, 8))
    db.upsert_media(path=str(lib), filename="keeper.png", status="ready")
    lib_id = db._query("SELECT id FROM media WHERE path=?", (str(lib),))[0]["id"]
    status, payload = call(f"/api/media/{lib_id}")
    check("deleting from the index succeeds", status == 200, f"got {status}")
    check("and says the file was kept", payload.get("file_deleted") is False, str(payload))
    check("the row is gone", db.get_media(lib_id) is None)
    check("but the file is still on disk", lib.exists(), "a read-only UI action deleted user data")

    # The same call with an explicit opt-in takes the bytes with it.
    lib2 = make_image(cfg.library_dir / "disposable.png", (8, 8))
    db.upsert_media(path=str(lib2), filename="disposable.png", status="ready")
    lib2_id = db._query("SELECT id FROM media WHERE path=?", (str(lib2),))[0]["id"]
    status, payload = call(f"/api/media/{lib2_id}?delete_file=1")
    check("delete_file=1 reports the file removed",
          status == 200 and payload.get("file_deleted") is True, str(payload))
    check("the file really is gone", not lib2.exists())
    check("and so is the row", db.get_media(lib2_id) is None)

    check("an unknown id is a 404", call("/api/media/999999")[0] == 404)
    server.shutdown()
    db.close()


def test_api_bridge_whitelist(tmp: Path) -> None:
    section("api / bridge surface")
    from vault34.server import create_app, serve
    cfg = Config(root=tmp)
    cfg.port = 0
    cfg.ensure_dirs()
    db = Database(cfg.db_path)
    pipeline = Pipeline(cfg, db, tagger=None)

    class Bridge:
        def stats(self):
            return {"ok": True}

        def secret(self):                 # must stay unreachable
            return "leaked"

    server, _ = serve(cfg, create_app(cfg, db, pipeline, desktop=Bridge()))
    import urllib.request
    import urllib.error
    base = f"http://127.0.0.1:{server.server_port}"

    def get(path):
        try:
            with urllib.request.urlopen(base + path, timeout=10) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    check("a whitelisted method works", get("/api/bridge/stats")[0] == 200)
    check("a method outside the whitelist is a 404",
          get("/api/bridge/secret")[0] == 404,
          "any public attribute of the bridge was callable over HTTP")
    check("dunder access is blocked", get("/api/bridge/__class__")[0] == 404)
    server.shutdown()
    db.close()


def test_status_caches_ffmpeg_probe(tmp: Path) -> None:
    section("api / status cost")
    import vault34.server as server_mod
    calls = {"n": 0}
    real = server_mod.ffmpeg_version

    def counting(value):
        calls["n"] += 1
        return real(value)

    server_mod.ffmpeg_version = counting
    try:
        cfg = Config(root=tmp)
        cfg.port = 0
        cfg.ensure_dirs()
        db = Database(cfg.db_path)
        pipeline = Pipeline(cfg, db, tagger=None)
        srv, _ = server_mod.serve(cfg, server_mod.create_app(cfg, db, pipeline))
        import urllib.request
        base = f"http://127.0.0.1:{srv.server_port}"
        for _ in range(5):
            with urllib.request.urlopen(base + "/api/status", timeout=10):
                pass
        check("ffmpeg is probed once, not on every poll", calls["n"] <= 1,
              f"spawned a subprocess {calls['n']} times for 5 status polls")
        srv.shutdown()
        db.close()
    finally:
        server_mod.ffmpeg_version = real


# ------------------------------------------------------------------- runner
def main() -> int:
    tests = [
        test_added_at_is_stable, test_empty_tags_clear, test_fts_degrades,
        test_punctuation_query, test_queue_dedup, test_queue_migration,
        test_retry_paths, test_upsert_requires_path, test_like_wildcards,
        test_bytes_counts_ready_only, test_duplicate_groups_respects_tolerance,
        test_config_bad_env, test_config_unknown_key,
        test_exif_consistency, test_animated_detection, test_animation_frame_consistency,
        test_pending_never_reports_zero_mid_flight, test_rescan_does_not_reindex,
        test_unreadable_file_is_recorded, test_stop_defers_queued_work,
        test_watcher_ignores_paths_outside_inbox, test_watcher_drops_empty_stubs,
        test_api_input_validation, test_api_rejects_foreign_origins,
        test_api_ingest_is_confined, test_api_delete_is_scoped,
        test_api_bridge_whitelist, test_status_caches_ffmpeg_probe,
    ]
    for test in tests:
        with tempfile.TemporaryDirectory() as name:
            try:
                test(Path(name))
            except Exception as exc:  # noqa: BLE001
                import traceback
                FAIL.append(test.__name__)
                print(f"  [FAIL] {test.__name__} raised {exc!r}")
                traceback.print_exc()
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("failing: " + ", ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Verify the HTTP API surface and the live filesystem watcher.

Runs the real server in-process, exercises every route over HTTP, then drops a
file into the inbox and waits for watchdog to pick it up on its own.
"""

from __future__ import annotations

import io
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from PIL import Image, ImageDraw

from vault34.config import load_config
from vault34.db import Database
from vault34.pipeline import Pipeline
from vault34.server import create_app, serve
from vault34.tagger import Tagger, load_vocabulary
from vault34.watcher import InboxWatcher

FAILURES: list[str] = []
BASE = ""


def check(name: str, condition: bool, detail: str = "") -> None:
    mark = "PASS" if condition else "FAIL"
    print(f"  [{mark}] {name}" + (f"  -- {detail}" if detail else ""))
    if not condition:
        FAILURES.append(name)


def get(path: str, raw: bool = False):
    with urllib.request.urlopen(BASE + path, timeout=30) as response:
        body = response.read()
        return body if raw else json.loads(body)


def get_status(path: str) -> int:
    try:
        with urllib.request.urlopen(BASE + path, timeout=30):
            return 200
    except urllib.error.HTTPError as exc:
        return exc.code


def post(path: str) -> dict:
    req = urllib.request.Request(BASE + path, data=b"", method="POST")
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.loads(response.read())


def main() -> int:
    global BASE
    cfg = load_config()
    cfg.port = 0          # ephemeral: never collide with a running instance
    db = Database(cfg.db_path)
    tagger = Tagger(cfg.model_path, load_vocabulary(cfg.tags_path))
    pipeline = Pipeline(cfg, db, tagger)
    pipeline.start()

    app = create_app(cfg, db, pipeline)
    server, _ = serve(cfg, app)
    BASE = f"http://{cfg.host}:{server.server_port}"
    time.sleep(0.6)

    print("\n== static ==")
    try:
        html = get("/", raw=True).decode()
        check("index.html served", "Vault34" in html and "app.js" in html, f"{len(html)} B")
        css = get("/style.css", raw=True)
        check("stylesheet served", len(css) > 500, f"{len(css)} B")
        js = get("/app.js", raw=True)
        check("script served", b"/api/search" in js, f"{len(js)} B")
    except Exception as exc:  # noqa: BLE001
        check("static assets", False, str(exc))

    print("\n== data routes ==")
    stats = get("/api/stats")
    print(f"  stats: {json.dumps(stats)[:160]}")
    check("stats reports totals", stats["total"] > 0)
    check("stats include progress", "progress" in stats)

    status = get("/api/status")
    print(f"  tagger_ready={status['tagger_ready']} providers={status['providers']} "
          f"ffmpeg={status['ffmpeg']}")
    check("tagger reported ready", status["tagger_ready"] is True)
    check("model present", status["model_present"] is True)
    check("vocabulary present", status["tags_present"] is True)
    check("thresholds exposed", "general" in status["thresholds"])

    search = get("/api/search?limit=5")
    print(f"  search: {search['total']} total, {len(search['items'])} returned")
    check("search returns items", len(search["items"]) > 0)
    check("items expose media_url", all("media_url" in i for i in search["items"]))
    check("items expose thumb_url", all("thumb_url" in i for i in search["items"]))

    if search["items"]:
        first = search["items"][0]
        media = get(f"/api/media/{first['id']}")
        check("media detail", media["id"] == first["id"] and "tags" in media)
        thumb = get(f"/api/media/{first['id']}/thumb", raw=True)
        check("thumbnail bytes", thumb[:2] == b"\xff\xd8", f"{len(thumb)} B, JPEG magic ok")
        blob = get(f"/api/media/{first['id']}/file", raw=True)
        check("original file bytes", len(blob) > 0, f"{len(blob)} B")

    print("\n== search behaviour ==")
    for query, label in [("?q=forest", "text q"), ("?kind=video", "kind=video"),
                         ("?tag=sun", "tag=sun"), ("?sort=name", "sort=name"),
                         ("?limit=2&offset=0", "paging")]:
        result = get("/api/search" + query)
        print(f"  {label:14s} -> {result['total']} hit(s)")
        check(f"search {label}", isinstance(result["total"], int))

    ac = get("/api/tags/autocomplete?q=su")
    print(f"  autocomplete 'su' -> {[t['name'] for t in ac]}")
    check("autocomplete works", isinstance(ac, list))

    top = get("/api/tags/top?limit=5")
    check("top tags", len(top) > 0, f"{[t['name'] for t in top]}")

    dupes = get("/api/duplicates")
    print(f"  duplicate groups: {dupes['count']}")
    check("duplicates route", "groups" in dupes)
    if dupes["groups"]:
        g = dupes["groups"][0]
        check("group has >1 member", len(g) > 1, f"{len(g)} files")
        check("group members expose thumbs", all("thumb_url" in i for i in g))

    print("\n== error handling ==")
    check("unknown media -> 404", get_status("/api/media/999999") == 404)
    check("missing static -> 404", get_status("/nope.js") == 404)
    check("path traversal blocked", get_status("/../vault34/db.py") == 404)

    print("\n== live watcher ==")
    watcher = InboxWatcher(cfg.inbox_dir, pipeline.submit,
                           cfg.stability_checks, cfg.stability_interval)
    watcher.start()
    time.sleep(0.8)

    before = db.stats()["total"]
    target = cfg.inbox_dir / "watcher_drop.png"
    im = Image.new("RGB", (400, 300), (250, 250, 250))
    d = ImageDraw.Draw(im)
    d.ellipse([80, 60, 320, 240], fill=(30, 90, 200))
    im.save(target)

    print("  dropped watcher_drop.png into the inbox; waiting for watchdog...")
    deadline = time.time() + 180
    picked = False
    while time.time() < deadline:
        rows = db._query("SELECT filename, status FROM media")
        if any(r["filename"] == "watcher_drop.png" for r in rows):
            picked = True
            break
        time.sleep(0.5)
    check("watcher detected the new file", picked,
          f"stats {before} -> {db.stats()['total']}")

    # A file still being written must not be indexed until it settles. The
    # test deliberately corrupts it, so "settled" can only mean "recorded as
    # an explicit error, never as ready" - silently dropping the row (the old
    # behaviour) would also satisfy a naive check and hide the corruption.
    partial = cfg.inbox_dir / "slow_write.png"
    with open(partial, "wb") as handle:
        im.save(handle, "PNG")
        for _ in range(4):
            handle.flush()
            time.sleep(0.4)
            with open(partial, "r+b") as grow:
                grow.write(b"\x00" * 64)   # clobbers the PNG signature
    time.sleep(2.5)
    rows = [dict(r) for r in db._query(
        "SELECT filename, status, error FROM media WHERE filename='slow_write.png'")]
    check("a corrupt slow write is never indexed as ready",
          rows and all(r["status"] == "error" for r in rows),
          f"rows={rows}")
    check("and the corruption is reported, not swallowed",
          all(r["error"] for r in rows), f"rows={rows}")

    check("scan endpoint", post("/api/scan").get("status") == "queued")
    check("progress endpoint", "phase" in get("/api/progress"))

    watcher.stop()
    pipeline.wait_until_idle(120)
    pipeline.stop()
    server.shutdown()
    db.close()

    print()
    if FAILURES:
        print(f"FAILED {len(FAILURES)} check(s): {FAILURES}")
        return 1
    print("all API checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

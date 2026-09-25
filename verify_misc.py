"""Verify config persistence, the desktop bridge, and matcher rebuild.

These paths are only exercised through the GUI in normal use, so they get
their own checks against a throwaway project root.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from vault34.config import Config, load_config
from vault34.db import Database
from vault34.pipeline import Pipeline
from vault34.server import create_app, serve

FAILURES: list[str] = []
BASE = ""


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if condition else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))
    if not condition:
        FAILURES.append(name)


def get(path: str) -> tuple[int, str]:
    return _call("GET", path)


def post(path: str) -> tuple[int, str]:
    return _call("POST", path)


def _call(method: str, path: str) -> tuple[int, str]:
    request = urllib.request.Request(BASE + path, method=method, data=b"" if method == "POST" else None)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def test_config(tmp: Path) -> None:
    print("\n== config ==")
    cfg = Config(root=tmp)
    check("defaults are derived from root", cfg.inbox_dir == tmp / "media" / "inbox",
          str(cfg.inbox_dir))
    check("db path is portable", cfg.db_path == tmp / "vault34.db")

    cfg.inbox_dir = tmp / "custom-inbox"
    cfg.library_dir = tmp / "custom-library"
    check("inbox setter works", cfg.inbox_dir == (tmp / "custom-inbox").resolve(),
          str(cfg.inbox_dir))
    cfg.save()
    saved = (tmp / "config.json").is_file()
    check("config.json written", saved)

    reloaded = load_config(tmp)
    check("inbox survives reload", reloaded.inbox_dir == (tmp / "custom-inbox").resolve(),
          str(reloaded.inbox_dir))
    check("library survives reload", reloaded.library_dir == (tmp / "custom-library").resolve(),
          str(reloaded.library_dir))
    check("to_dict exposes both", "inbox_dir" in reloaded.to_dict())

    plain = load_config(tmp / "elsewhere")
    check("fresh root uses defaults",
          plain.inbox_dir == (tmp / "elsewhere" / "media" / "inbox"), str(plain.inbox_dir))

    (tmp / "broken.json").write_text("{ not json", encoding="utf-8")
    (tmp / "config.json").write_text("{ not json", encoding="utf-8")
    try:
        recovered = load_config(tmp)
        check("unreadable config.json does not crash", recovered.root == tmp.resolve(),
              str(recovered.root))
    except Exception as exc:  # noqa: BLE001
        check("unreadable config.json does not crash", False, str(exc))


def test_matcher_rebuild(tmp: Path) -> None:
    print("\n== matcher rebuild from the database ==")
    db = Database(tmp / "matcher.db")
    db.upsert_media(path=tmp / "library" / "a.png", filename="a.png", ext=".png",
                    kind="image", sha256="deadbeef" * 8, phash="0" * 16, dhash="0" * 16,
                    status="ready")
    rows = [dict(r) for r in db.iter_hashes()]
    check("iter_hashes returns sha256", rows and rows[0]["sha256"] == "deadbeef" * 8,
          str(rows[0].get("sha256"))[:24] if rows else "no rows")

    cfg = Config(root=tmp)
    pipeline = Pipeline(cfg, db, None)
    check("exact hashes loaded into matcher", "deadbeef" * 8 in pipeline.matcher._by_sha,
          f"{len(pipeline.matcher._by_sha)} sha / {len(pipeline.matcher._by_bucket)} buckets")

    # A sha-only row (frame hashing failed) must still be exact-matchable.
    db.upsert_media(path=tmp / "library" / "b.mp4", filename="b.mp4", ext=".mp4",
                    kind="video", sha256="cafebabe" * 8, phash=None, dhash=None,
                    status="ready")
    rows = [dict(r) for r in db.iter_hashes()]
    check("sha-only rows are included", any(r["sha256"] == "cafebabe" * 8 for r in rows),
          f"{len(rows)} rows")
    db.close()


def test_upsert(tmp: Path) -> None:
    print("\n== upsert ==")
    db = Database(tmp / "upsert.db")
    a = tmp / "lib" / "x.png"
    mid = db.upsert_media(path=a, filename="x.png", ext=".png", kind="image",
                          status="ready", tags=[("sun", "general", 0.9)])
    row = db.get_media(mid)
    check("path stored verbatim as a string", row["path"] == str(a), row["path"])
    check("tags written", [t["name"] for t in row["tags"]] == ["sun"])

    # re-upserting the same path updates rather than duplicating
    mid2 = db.upsert_media(path=a, filename="x.png", ext=".png", kind="image",
                           status="ready", rating=3)
    check("same path updates in place", mid2 == mid and db.stats()["total"] == 1,
          f"total={db.stats()['total']}")

    b = tmp / "lib" / "y.png"
    mid3 = db.upsert_media(path=b, filename="y.png", ext=".png", kind="image",
                           status="ready", tags=[])
    check("empty tag list clears tags", db.get_media(mid3)["tags"] == [])
    db.close()


def test_bridge(tmp: Path) -> None:
    global BASE
    print("\n== desktop bridge route ==")
    cfg = Config(root=tmp)
    cfg.port = 0          # ephemeral: never collide with a running instance
    db = Database(cfg.db_path)
    pipeline = Pipeline(cfg, db, None)

    class FakeDesktop:
        def stats(self):
            return db.stats()

        def reveal(self, media_id):
            return f"revealed {media_id}"

        def reveal_folder(self, which):
            raise RuntimeError("kaboom")

        def _private(self):
            return "nope"

    app = create_app(cfg, db, pipeline, desktop=FakeDesktop())
    server, _ = serve(cfg, app)
    BASE = f"http://{cfg.host}:{server.server_port}"
    time.sleep(0.6)
    try:
        code, body = get("/api/bridge/stats")
        check("no-arg method works", code == 200 and "total" in body, f"{code}")

        code, body = post("/api/bridge/reveal?id=42")
        check("arg forwarded from query string", code == 200 and "revealed 42" in body,
              body[:80])

        code, body = post("/api/bridge/reveal?id=42")
        check("named arg forwarded", code == 200 and "revealed 42" in body, body[:80])

        code, _ = post("/api/bridge/reveal")
        check("missing required arg -> 400", code == 400, f"{code}")

        code, _ = get("/api/bridge/stats?bogus=1")
        check("unexpected arg -> 400", code == 400, f"{code}")

        code, _ = get("/api/bridge/_private")
        check("private method blocked", code == 404, f"{code}")

        code, _ = get("/api/bridge/nope")
        check("unknown method -> 404", code == 404, f"{code}")

        code, body = post("/api/bridge/reveal_folder?which=inbox")
        check("raising method -> JSON 500", code == 500 and "kaboom" in body, f"{code} {body[:60]}")

        code, _ = get("/api/bridge/boom")
        check("method outside the whitelist is a 404", code == 404,
              f"{code} - a method not on BRIDGE_METHODS was reachable over HTTP")

        # A cross-origin GET is a "simple request": no preflight, so the
        # browser sends it even to a server with no CORS headers. Anything that
        # touches the OS must be POST-only or a hostile <img> can drive it.
        for name in ("reveal", "reveal_folder", "set_folder", "rescan"):
            code, _ = get(f"/api/bridge/{name}")
            check(f"GET /api/bridge/{name} is refused", code == 405,
                  f"{code} - reachable from a cross-origin <img> tag")
    finally:
        server.shutdown()
        db.close()


def test_real_bridge(tmp: Path) -> None:
    """The real DesktopBridge, not a stand-in: this is what the buttons call."""
    global BASE
    print("\n== real desktop bridge ==")
    import main as app_main

    cfg = Config(root=tmp)
    cfg.port = 0          # ephemeral: never collide with a running instance
    db = Database(cfg.db_path)
    pipeline = Pipeline(cfg, db, None)
    media = tmp / "library" / "shot.png"
    db.upsert_media(path=media, filename="shot.png", ext=".png", kind="image",
                    status="ready", width=10, height=10)

    opened: list = []
    revealed: list = []
    original_open = app_main._open
    original_reveal = app_main.open_in_explorer
    app_main._open = lambda folder: (opened.append(str(folder)), True)[1]
    app_main.open_in_explorer = lambda path: (revealed.append(str(path)), True)[1]

    try:
        app = create_app(cfg, db, pipeline,
                         desktop=app_main.DesktopBridge(cfg, pipeline, None))
        server, _ = serve(cfg, app)
        BASE = f"http://{cfg.host}:{server.server_port}"
        time.sleep(0.6)
        try:
            code, body = get("/api/bridge/stats")
            check("stats() works", code == 200 and "total" in body, f"{code}")

            code, body = post("/api/bridge/reveal_folder?which=inbox")
            check("'inbox' button reaches the bridge",
                  code == 200 and str(cfg.inbox_dir) in opened, f"{code} {opened}")

            code, body = post("/api/bridge/reveal_folder?which=library")
            check("library folder opens", code == 200 and str(cfg.library_dir) in opened,
                  f"{code} {opened}")

            code, _ = post("/api/bridge/reveal_folder?which=bogus")
            check("unknown folder is a no-op, not a crash", code == 200, f"{code}")

            code, _ = post("/api/bridge/rescan")
            check("rescan works", code == 200, f"{code}")

            code, _ = post("/api/bridge/reveal?id=1")
            check("'Show in folder' reaches the file",
                  code == 200 and revealed and revealed[0].endswith("shot.png"),
                  f"{code} {revealed}")
        finally:
            server.shutdown()
    finally:
        app_main._open = original_open
        app_main.open_in_explorer = original_reveal
        db.close()


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="vault34-misc-"))
    try:
        test_config(tmp)
        test_matcher_rebuild(tmp)
        test_upsert(tmp)
        test_bridge(tmp)
        test_real_bridge(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if FAILURES:
        print(f"FAILED {len(FAILURES)} check(s): {FAILURES}")
        return 1
    print("all config/bridge checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

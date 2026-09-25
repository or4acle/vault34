"""Local HTTP API + static hosting for the pywebview window.

The server is bound to the loopback interface only. File access is mediated by
media id (never by a client-supplied path), so a stray request cannot read
arbitrary files off the machine.
"""

from __future__ import annotations

import functools
import inspect
import json
import threading
import webbrowser
from pathlib import Path
from urllib.parse import urlsplit

from flask import (Flask, Response, abort, jsonify, request, send_file,
                   send_from_directory)
from werkzeug.exceptions import BadRequest, HTTPException

from .config import IMAGE_EXT, VIDEO_EXT, Config
from .db import Database
from .media import ffmpeg_version
from .pipeline import Pipeline

# Only these may be driven from a URL. Resolving any public attribute of the
# bridge meant a new method added for the desktop was automatically exposed to
# anything that could reach the loopback port.
BRIDGE_METHODS = frozenset({"reveal", "reveal_folder", "rescan", "set_folder", "stats"})

# Bridge calls that touch the OS or rescan the inbox, so they must not be
# triggerable by a third-party page that happens to know the port. These require
# POST: a cross-origin GET is a "simple request" the browser will send without
# any preflight, so <img src="/api/bridge/reveal_folder?which=inbox"> on a
# hostile page would otherwise open the user's folders. POST triggers a CORS
# preflight that _local_only refuses, and the browser never sends the request.
BRIDGE_MUTATIONS = frozenset({"reveal", "reveal_folder", "set_folder", "rescan"})


def _coerce(value: str, annotation):
    """Turn a query-string value into the type the callee declared."""
    if annotation is int:
        try:
            return int(value)
        except ValueError:
            raise BadRequest(f"expected an integer, got {value!r}")
    if annotation is float:
        try:
            return float(value)
        except ValueError:
            raise BadRequest(f"expected a number, got {value!r}")
    if annotation is bool:
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return value


def _int_arg(name: str, default: int, low: int, high: int) -> int:
    """Read a bounded integer query parameter.

    ``int()`` straight on ``request.args`` raised ValueError, which Flask turns
    into an HTML 500. A malformed query should be a 400 in the same JSON shape
    as every other response, and a negative limit meant "unlimited" to SQLite.
    """
    raw = request.args.get(name)
    if raw is None or raw == "":
        value = default
    else:
        try:
            value = int(raw)
        except ValueError:
            raise BadRequest(f"'{name}' must be an integer, got {raw!r}")
    return max(low, min(high, value))


def _float_arg(name: str, default: float, low: float, high: float) -> float:
    raw = request.args.get(name)
    if raw is None or raw == "":
        value = default
    else:
        try:
            value = float(raw)
        except ValueError:
            raise BadRequest(f"'{name}' must be a number, got {raw!r}")
    return max(low, min(high, value))


def create_app(cfg: Config, db: Database, pipeline: Pipeline,
               desktop=None) -> Flask:
    app = Flask(__name__, static_folder=None)
    # Flask 2.3 dropped the JSON_SORT_KEYS config key in favour of this
    # attribute, so the old assignment was silently ignored.
    app.json.sort_keys = False

    @app.errorhandler(HTTPException)
    def _http_error(exc: HTTPException):
        """Errors are JSON, not Flask's default HTML page.

        The frontend fetches these endpoints and does response.json(); an HTML
        error body would surface as an opaque "Unexpected token <" in the
        console instead of the message we deliberately wrote.
        """
        return jsonify({"error": exc.description, "status": exc.code}), exc.code

    @app.before_request
    def _local_only():
        """Reject requests that did not come from this app on this machine.

        The API is unauthenticated and bound to loopback, so without this any
        page the user visits could POST to /api/scan or drive the bridge with
        an <img> tag. A cross-origin request either omits Host or carries one
        that is not ours; the Origin check additionally covers the simple
        GET-based bridge calls.
        """
        host = request.host.split(":")[0].strip("[]").lower()
        allowed = {cfg.host, "127.0.0.1", "localhost", "::1"}
        if host not in allowed:
            return jsonify({"error": "bad host"}), 403
        origin = request.headers.get("Origin")
        if origin:
            parsed = urlsplit(origin)
            if parsed.hostname not in allowed:
                return jsonify({"error": "cross-origin request refused"}), 403
        return None

    def safe_media_file(media_id: int, what: str):
        item = db.get_media(media_id)
        if not item:
            abort(404)
        if what == "thumb":
            rel = item.get("thumb")
            if not rel:
                abort(404)
            candidate = (cfg.thumbs_dir / rel).resolve()
            if not candidate.is_file() or cfg.thumbs_dir.resolve() not in candidate.parents:
                abort(404)
            return send_file(candidate, mimetype="image/jpeg", max_age=86400)
        path = Path(item["path"])
        if not path.is_file():
            abort(404)
        return send_file(path, conditional=True)

    # -- static ---------------------------------------------------------
    @app.after_request
    def _no_store(resp):
        if request.path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.route("/")
    def index():
        return send_from_directory(cfg.web_dir, "index.html")

    @app.route("/<path:filename>")
    def static_files(filename: str):
        target = (cfg.web_dir / filename).resolve()
        if cfg.web_dir.resolve() not in target.parents or not target.is_file():
            abort(404)
        return send_from_directory(cfg.web_dir, filename)

    # -- data -----------------------------------------------------------
    @app.get("/api/stats")
    def api_stats():
        stats = db.stats()
        stats["queue"] = pipeline.pending
        stats["progress"] = pipeline.progress.snapshot()
        return jsonify(stats)

    @functools.lru_cache(maxsize=None)
    def _ffmpeg_version_cached(value: str | None) -> str | None:
        return ffmpeg_version(value)

    @app.get("/api/status")
    def api_status():
        return jsonify({
            "root": str(cfg.root),
            "inbox": str(cfg.inbox_dir),
            "library": str(cfg.library_dir),
            "duplicates": str(cfg.duplicates_dir),
            "model": str(cfg.model_path),
            "model_present": cfg.model_path.is_file(),
            "tags_present": cfg.tags_path.is_file(),
            "tagger_ready": bool(pipeline.tagger and pipeline.tagger.ready),
            "providers": getattr(pipeline.tagger, "providers", []),
            "ffmpeg": pipeline.ffmpeg,
            # Shelling out to `ffmpeg -version` took up to 10s and ran on every
            # poll of this endpoint; the answer cannot change while the app is
            # up, so it is computed once per ffmpeg path.
            "ffmpeg_version": _ffmpeg_version_cached(pipeline.ffmpeg),
            "video_backend": "opencv",
            "has_fts": db.has_fts,
            "thresholds": {
                "general": cfg.general_threshold,
                "character": cfg.character_threshold,
                "max_tags": cfg.max_tags,
            },
            "organize": cfg.organize,
        })

    @app.get("/api/search")
    def api_search():
        tags = [t for t in (request.args.getlist("tag") or []) if t]
        for chunk in (request.args.get("tags") or "").split(","):
            tags.extend(t.strip() for t in chunk.split(",") if t.strip())
        kinds = [k for k in (request.args.getlist("kind") or []) if k]
        result = db.search(
            query=request.args.get("q", "").strip(),
            tags=tags,
            kinds=kinds,
            limit=_int_arg("limit", 60, 1, 500),
            offset=_int_arg("offset", 0, 0, 1_000_000),
            min_confidence=_float_arg("min_confidence", 0.0, 0.0, 1.0),
            sort=request.args.get("sort", "recent"),
        )
        result["query"] = {"tags": tags, "kinds": kinds}
        for item in result["items"]:
            serialize(item)
        return jsonify(result)

    @app.get("/api/tags/autocomplete")
    def api_autocomplete():
        return jsonify(db.autocomplete(
            request.args.get("q", ""), _int_arg("limit", 25, 1, 200)))

    @app.get("/api/tags/top")
    def api_top_tags():
        return jsonify(db.top_tags(_int_arg("limit", 40, 1, 500),
                                   request.args.get("category") or None))

    @app.get("/api/tags/related")
    def api_related():
        name = request.args.get("name", "")
        if not name:
            return jsonify([])
        return jsonify(db.similar_tags(name, _int_arg("limit", 12, 1, 200)))

    def serialize(item: dict) -> dict:
        """Add the derived fields every client needs.

        Search, detail and duplicates all render the same cards, so the URL
        construction lives here instead of being repeated per route.
        """
        media_id = item.get("id")
        item["media_url"] = f"/api/media/{media_id}/file"
        item["thumb_url"] = (f"/api/media/{media_id}/thumb" if item.get("thumb")
                             else f"/api/media/{media_id}/file")
        return item

    @app.get("/api/media/<int:media_id>")
    def api_media(media_id: int):
        item = db.get_media(media_id)
        if not item:
            abort(404)
        serialize(item)
        item["exists"] = Path(item["path"]).is_file()
        return jsonify(item)

    @app.get("/api/media/<int:media_id>/thumb")
    def api_thumb(media_id: int):
        return safe_media_file(media_id, "thumb")

    @app.get("/api/media/<int:media_id>/file")
    def api_file(media_id: int):
        return safe_media_file(media_id, "file")

    @app.get("/api/duplicates")
    def api_duplicates():
        groups = db.duplicate_groups()
        for group in groups:
            for item in group:
                serialize(item)
        return jsonify({"groups": groups, "count": len(groups)})

    @app.get("/api/progress")
    def api_progress():
        return jsonify(pipeline.progress.snapshot())

    @app.post("/api/scan")
    def api_scan():
        found = pipeline.scan_inbox()
        return jsonify({"status": "queued", "found": found,
                        "queue": pipeline.pending})

    def _sanitise_folder(raw: str) -> Path:
        """Resolve a client-supplied folder and refuse anything out of bounds.

        Ingest *moves* files into the library, so an unvalidated path let any
        request hand the pipeline ``C:\\`` and have the app relocate the user's
        entire disk. Only the inbox and the library are accepted, and both are
        compared after symlink resolution.
        """
        try:
            target = Path(raw).expanduser().resolve()
        except (OSError, ValueError):
            raise BadRequest("unusable folder path")
        roots = [cfg.inbox_dir.resolve(), cfg.library_dir.resolve()]
        if not any(target == root or target.is_relative_to(root) for root in roots):
            raise BadRequest(
                f"folder must be inside the inbox or library, not {target}")
        return target

    @app.post("/api/ingest")
    def api_ingest():
        payload = request.get_json(silent=True) or {}
        folder = payload.get("folder")
        if folder:
            target = _sanitise_folder(str(folder))
            media = [p for p in sorted(target.rglob("*"))
                     if p.is_file() and p.suffix.lower() in (IMAGE_EXT | VIDEO_EXT)]
            pipeline.submit_many(media)
            return jsonify({"status": "queued", "found": len(media),
                            "queue": pipeline.pending})
        found = pipeline.scan_inbox()
        return jsonify({"status": "queued", "found": found,
                        "queue": pipeline.pending})

    @app.delete("/api/media/<int:media_id>")
    def api_delete(media_id: int):
        """Forget an entry, optionally deleting the file it points at.

        Only reachable for rows inside the library or the duplicates folder, so
        a malformed database entry cannot be turned into an arbitrary delete.
        """
        item = db.get_media(media_id)
        if not item:
            abort(404)
        path = Path(item["path"])
        try:
            managed = path.resolve().is_relative_to(
                cfg.library_dir.resolve()) or path.resolve().is_relative_to(
                cfg.duplicates_dir.resolve())
        except OSError:
            managed = False
        if not managed:
            abort(400, "refusing to touch a file outside the library")
        if request.args.get("delete_file") in {"1", "true", "yes"}:
            # Unlink first: if the filesystem refuses, the caller keeps a row
            # pointing at a file that still exists, which is recoverable.
            # Deleting the row first would lose the only handle on that file.
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                return jsonify({"status": "delete_failed",
                                "file_deleted": False, "error": str(exc)}), 500
            db.delete_media(media_id)
            return jsonify({"status": "deleted", "file_deleted": True})
        db.delete_media(media_id)
        return jsonify({"status": "removed_from_index", "file_deleted": False})

    # -- desktop bridge -------------------------------------------------
    def bridge_json(payload, status: int = 200) -> Response:
        """jsonify() rejects a `default=` hook, and bridge results are free-form
        (booleans, Path objects, ...), so serialise defensively ourselves."""
        return Response(json.dumps(payload, default=str), status=status,
                        mimetype="application/json")

    @app.route("/api/bridge/<name>", methods=["GET", "POST"])
    def api_bridge(name: str):
        """Call a DesktopBridge method from the web UI.

        The browser has no pywebview bridge, so the UI falls back to this route.
        Arguments come from the query string, matched by parameter name and then
        filled positionally, so ``?id=7`` can drive a ``media_id`` parameter.
        Values are coerced to the annotation the method declares, because a
        query string only ever yields text.

        Methods in BRIDGE_MUTATIONS must arrive as POST; see the note there.
        """
        if name in BRIDGE_MUTATIONS and request.method != "POST":
            # Checked before the bridge is even resolved so the guarantee holds
            # regardless of whether a desktop bridge is attached.
            return jsonify({"error": f"{name} requires POST"}), 405
        if desktop is None or name not in BRIDGE_METHODS:
            abort(404)
        method = getattr(desktop, name, None)
        if not callable(method):
            abort(404)

        try:
            params = [p for p in inspect.signature(method).parameters.values()
                      if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
        except (TypeError, ValueError):
            return bridge_json({"result": method()})

        supplied = dict(request.args)
        kwargs: dict = {}
        for param in params:
            if param.name in supplied:
                kwargs[param.name] = _coerce(supplied.pop(param.name), param.annotation)
            elif supplied:
                # e.g. `?id=7` for a parameter named `media_id`
                key = next(iter(supplied))
                kwargs[param.name] = _coerce(supplied.pop(key), param.annotation)
            elif param.default is param.empty:
                abort(400, f"missing argument '{param.name}'")
        if supplied:
            abort(400, f"unexpected argument(s): {', '.join(supplied)}")

        try:
            result = method(**kwargs)
        except TypeError as exc:
            abort(400, str(exc))
        except Exception as exc:  # noqa: BLE001 - report as JSON, not an HTML 500
            return bridge_json({"error": f"{type(exc).__name__}: {exc}"}, status=500)
        return bridge_json({"result": result})

    return app


def serve(cfg: Config, app: Flask):
    """Run the Flask app on a background thread; returns (server, thread).

    Pass ``port=0`` to bind an ephemeral port, then read ``server.server_port``.
    A busy port raises OSError with a message the caller can show verbatim.
    """
    from werkzeug.serving import make_server

    try:
        server = make_server(cfg.host, cfg.port, app, threaded=True)
    except OSError as exc:
        raise OSError(
            f"port {cfg.port} on {cfg.host} is already in use ({exc}). "
            f"Another Vault34 instance is probably running - close it, or start "
            f"this one with VAULT34_PORT=<other port>."
        ) from exc
    thread = threading.Thread(target=server.serve_forever, name="vault34-http",
                              daemon=True)
    thread.start()
    return server, thread


def open_in_explorer(path: str | Path) -> bool:
    import os
    import subprocess
    path = str(path)
    if not Path(path).exists():
        return False
    if os.name == "nt":
        try:
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
            return True
        except OSError:
            return False
    try:
        subprocess.Popen(["xdg-open", os.path.dirname(path)])
        return True
    except OSError:
        return False


def open_in_browser(url: str) -> None:
    threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()

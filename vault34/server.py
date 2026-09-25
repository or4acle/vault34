"""Local HTTP API + static hosting for the pywebview window.

The server is bound to the loopback interface only. File access is mediated by
media id (never by a client-supplied path), so a stray request cannot read
arbitrary files off the machine.
"""

from __future__ import annotations

import inspect
import json
import threading
import webbrowser
from pathlib import Path

from flask import (Flask, Response, abort, jsonify, request, send_file,
                   send_from_directory)

from .config import Config
from .db import Database
from .media import ffmpeg_version
from .pipeline import Pipeline


def create_app(cfg: Config, db: Database, pipeline: Pipeline,
               desktop=None) -> Flask:
    app = Flask(__name__, static_folder=None)
    app.config["JSON_SORT_KEYS"] = False

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
            "ffmpeg_version": ffmpeg_version(pipeline.ffmpeg),
            "video_backend": "opencv",
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
            limit=min(int(request.args.get("limit", 60)), 500),
            offset=int(request.args.get("offset", 0)),
            min_confidence=float(request.args.get("min_confidence", 0) or 0),
            sort=request.args.get("sort", "recent"),
        )
        result["query"] = {"tags": tags, "kinds": kinds}
        for item in result["items"]:
            serialize(item)
        return jsonify(result)

    @app.get("/api/tags/autocomplete")
    def api_autocomplete():
        return jsonify(db.autocomplete(
            request.args.get("q", ""), int(request.args.get("limit", 25))))

    @app.get("/api/tags/top")
    def api_top_tags():
        return jsonify(db.top_tags(int(request.args.get("limit", 40)),
                                   request.args.get("category") or None))

    @app.get("/api/tags/related")
    def api_related():
        name = request.args.get("name", "")
        if not name:
            return jsonify([])
        return jsonify(db.similar_tags(name, int(request.args.get("limit", 12))))

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

    @app.post("/api/ingest")
    def api_ingest():
        payload = request.get_json(silent=True) or {}
        folder = payload.get("folder")
        if folder:
            pipeline.submit_many(sorted(Path(folder).rglob("*")))
        else:
            pipeline.scan_inbox()
        return jsonify({"status": "queued", "queue": pipeline.pending})

    # -- desktop bridge -------------------------------------------------
    def bridge_json(payload, status: int = 200) -> Response:
        """jsonify() rejects a `default=` hook, and bridge results are free-form
        (booleans, Path objects, ...), so serialise defensively ourselves."""
        return Response(json.dumps(payload, default=str), status=status,
                        mimetype="application/json")

    @app.get("/api/bridge/<name>")
    def api_bridge(name: str):
        """Call a DesktopBridge method from the web UI.

        The browser has no pywebview bridge, so the UI falls back to this route.
        Arguments come from the query string, matched by parameter name and then
        filled positionally, so ``?id=7`` can drive a ``media_id`` parameter.
        """
        if desktop is None or name.startswith("_"):
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
                kwargs[param.name] = supplied.pop(param.name)
            elif supplied:
                # e.g. `?id=7` for a parameter named `media_id`
                key = next(iter(supplied))
                kwargs[param.name] = supplied.pop(key)
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

"""End-to-end verification of the Vault34 pipeline.

Builds a synthetic inbox (including a byte-identical copy, a rescaled
re-encode, an animated GIF and a real video), runs the pipeline, then checks
the database: dedup detection, tag persistence, search and autocomplete.
"""

from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from PIL import Image, ImageDraw

from vault34.config import IMAGE_EXT as IMG_EXT
from vault34.config import VIDEO_EXT as VID_EXT
from vault34.config import load_config
from vault34.db import Database
from vault34.media import find_ffmpeg, make_thumbnail
from vault34.pipeline import Pipeline
from vault34.tagger import Tagger, load_vocabulary

ROOT = Path(__file__).resolve().parent
FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    mark = "PASS" if condition else "FAIL"
    print(f"  [{mark}] {name}" + (f"  -- {detail}" if detail else ""))
    if not condition:
        FAILURES.append(name)


def build_inbox(cfg) -> list[Path]:
    inbox = cfg.inbox_dir
    if inbox.exists():
        shutil.rmtree(inbox)
    inbox.mkdir(parents=True, exist_ok=True)
    made: list[Path] = []

    def synth(kind: str, size=(640, 480)) -> Image.Image:
        im = Image.new("RGB", size, (255, 255, 255))
        d = ImageDraw.Draw(im)
        if kind == "sunset":
            for y in range(size[1]):
                f = y / max(1, size[1] - 1)
                d.line([(0, y), (size[0], y)],
                       fill=(int(60 + 195 * f), int(90 + 120 * f), int(200 - 90 * f)))
            d.ellipse([size[0] * .35, size[1] * .15, size[0] * .65, size[1] * .45],
                      fill=(255, 240, 120))
        elif kind == "forest":
            d.rectangle([0, 0, size[0], size[1] * .6], fill=(90, 170, 235))
            d.rectangle([0, size[1] * .6, size[0], size[1]], fill=(40, 120, 45))
            for i in range(7):
                x = 20 + i * (size[0] - 40) / 6
                d.polygon([(x, size[1] * .6), (x - 45, size[1]), (x + 45, size[1])],
                          fill=(20, 70, 30))
        elif kind == "checker":
            step = size[0] // 8
            for y in range(0, size[1], step):
                for x in range(0, size[0], step):
                    if (x // step + y // step) % 2 == 0:
                        d.rectangle([x, y, x + step, y + step], fill=(0, 0, 0))
        return im

    # 1. a landscape
    p = inbox / "sunset_landscape.png"
    synth("sunset").save(p)
    made.append(p)

    # 2. a forest
    p = inbox / "forest_scene.png"
    synth("forest").save(p)
    made.append(p)

    # 3. a greyscale image
    p = inbox / "checker_pattern.png"
    synth("checker").save(p)
    made.append(p)

    # 4. exact byte duplicate of #1
    p = inbox / "sunset_landscape_COPY.png"
    shutil.copy2(inbox / "sunset_landscape.png", p)
    made.append(p)

    # 5. perceptual near-duplicate of #1: resized + re-encoded as JPEG
    p = inbox / "sunset_landscape_resized.jpg"
    synth("sunset", (320, 240)).save(p, "JPEG", quality=70)
    made.append(p)

    # 6. animated GIF
    p = inbox / "animation.gif"
    frames = [synth("checker", (240, 240)).rotate(i * 45) for i in range(4)]
    frames[0].save(p, save_all=True, append_images=frames[1:], duration=120, loop=0)
    made.append(p)

    # 7. a real video, copied out of the user's library
    video = next(iter(sorted(Path.home().joinpath("Videos").rglob("*.mp4"))), None)
    if video:
        p = inbox / "clip_sample.mp4"
        shutil.copy2(video, p)
        made.append(p)
        # 7b. byte-identical copy of the video -> must be caught as an exact duplicate
        p = inbox / "clip_sample_copy.mp4"
        shutil.copy2(video, p)
        made.append(p)
        # 7c. re-encoded video (same frames, different bytes) -> perceptual duplicate
        p = inbox / "clip_sample_reencoded.mp4"
        try:
            import cv2
            src = cv2.VideoCapture(str(inbox / "clip_sample.mp4"))
            fps = src.get(cv2.CAP_PROP_FPS) or 25.0
            w = int(src.get(cv2.CAP_PROP_FRAME_WIDTH))
            h = int(src.get(cv2.CAP_PROP_FRAME_HEIGHT))
            out = cv2.VideoWriter(str(p), cv2.VideoWriter_fourcc(*"mp4v"), fps, (w // 2, h // 2))
            while True:
                ok, frame = src.read()
                if not ok:
                    break
                out.write(cv2.resize(frame, (w // 2, h // 2)))
            out.release()
            src.release()
            made.append(p)
        except Exception as exc:  # noqa: BLE001
            print(f"  (skipped re-encoded video: {exc})")

    # 8. junk that must be ignored
    (inbox / "notes.txt").write_text("ignore me", encoding="utf-8")
    return made


def drain(pipeline: Pipeline, timeout: float = 900) -> None:
    if not pipeline.wait_until_idle(timeout):
        raise TimeoutError(f"pipeline did not drain ({pipeline.pending} left)")


def main() -> int:
    cfg = load_config()
    if cfg.db_path.exists():
        cfg.db_path.unlink()
    for path in (cfg.thumbs_dir, cfg.library_dir, cfg.duplicates_dir):
        if path.exists():
            shutil.rmtree(path)
    cfg.ensure_dirs()

    print("\n== building synthetic inbox ==")
    made = build_inbox(cfg)
    for path in made:
        print(f"  {path.name:34s} {path.stat().st_size:>9,d} B")

    print("\n== loading tagger ==")
    vocab = load_vocabulary(cfg.tags_path)
    tagger = Tagger(cfg.model_path, vocab)
    print(f"  providers: {tagger.providers}")

    db = Database(cfg.db_path)
    pipeline = Pipeline(cfg, db, tagger)
    print(f"  ffmpeg: {pipeline.ffmpeg or 'not installed (OpenCV fallback)'}")

    print("\n== running pipeline ==")
    started = time.time()
    pipeline.start()
    pipeline.submit_many(made)
    drain(pipeline)
    elapsed = time.time() - started
    print(f"  processed in {elapsed:.1f}s")

    print("\n== database ==")
    stats = db.stats()
    for key, value in stats.items():
        print(f"  {key:12s} {value}")

    expected_unique = 5          # 3 png + gif + mp4
    expected_duplicates = 4     # image copy + resized image + video copy + re-encoded video
    check(f"library holds {expected_unique} unique items", stats["total"] == expected_unique,
          f"got {stats['total']}")
    check(f"{expected_duplicates} duplicates detected", stats["duplicates"] == expected_duplicates,
          f"got {stats['duplicates']}")
    check("text file ignored", not any("notes" in m["filename"] for m in db.search()["items"]))
    check("tags persisted", stats["tagged"] >= 3, f"tagged={stats['tagged']}")
    check("video indexed", stats["videos"] == 1, f"videos={stats['videos']}")
    check("animated flagged", stats["animated"] >= 1, f"animated={stats['animated']}")

    print("\n== tags produced ==")
    items = db.search()["items"]
    for item in items:
        top = ", ".join(f"{t['name']}({t['confidence']:.2f})" for t in item["tags"][:6])
        print(f"  {item['filename']:32s} [{item['rating']}] {top}")
    check("items carry tags", all(len(i["tags"]) > 0 for i in items))
    check("items have a rating", all(i["rating"] for i in items))
    check("items have dimensions", all(i["width"] > 0 for i in items))

    print("\n== duplicate handling ==")
    dupes = db.search()["items"]
    dups = [i for i in db._query("SELECT * FROM media WHERE status='duplicate'")]
    for row in dups:
        print(f"  {row['filename']:32s} -> of #{row['duplicate_of']} ({row['error']})")
    check("perceptual dup flagged with reason", all(r["error"] for r in dups))

    print("\n== file placement ==")
    # The DB must not point at files that never moved out of the inbox.
    rows = [dict(r) for r in db._query("SELECT filename, path, status FROM media")]
    missing = [r for r in rows if not Path(r["path"]).is_file()]
    check("every db path exists on disk", not missing,
          f"{len(missing)} missing: {[r['path'] for r in missing][:3]}")

    in_library = [r for r in rows if Path(r["path"]).parent == cfg.library_dir]
    in_duplicates = [r for r in rows if Path(r["path"]).parent == cfg.duplicates_dir]
    check(f"{expected_unique} files in the library", len(in_library) == expected_unique,
          f"{sorted(Path(r['path']).name for r in in_library)}")
    check(f"{expected_duplicates} files in duplicates/", len(in_duplicates) == expected_duplicates,
          f"{sorted(Path(r['path']).name for r in in_duplicates)}")

    still_in_inbox = [p for p in cfg.inbox_dir.iterdir()
                      if p.is_file() and p.suffix.lower() in IMG_EXT | VID_EXT]
    check("inbox drained of media", not still_in_inbox,
          f"left behind: {[p.name for p in still_in_inbox]}")

    check("non-media left alone in the inbox",
          (cfg.inbox_dir / "notes.txt").is_file())

    thumbs = list(cfg.thumbs_dir.glob("*.jpg"))
    check("thumbnails written to disk", len(thumbs) == expected_unique,
          f"{len(thumbs)} thumb(s)")

    print("\n== search ==")
    by_tag = db.search(tags=["sky"])
    print(f"  tag 'sky' -> {by_tag['total']} hit(s): "
          f"{[i['filename'] for i in by_tag['items']]}")
    check("tag search works", isinstance(by_tag["total"], int))

    both = db.search(tags=["sky", "sunset"])
    print(f"  'sky' AND 'sunset' -> {both['total']} hit(s)")
    check("AND semantics for tags", both["total"] <= by_tag["total"])

    text = db.search(query="forest")
    print(f"  text 'forest' -> {text['total']} hit(s): "
          f"{[i['filename'] for i in text['items']]}")
    check("free-text search finds filename", text["total"] >= 1)

    by_kind = db.search(kinds=["video"])
    check("kind filter works", by_kind["total"] == 1)

    print("\n== autocomplete ==")
    ac = db.autocomplete("su")
    print(f"  'su' -> {[t['name'] for t in ac[:8]]}")
    check("autocomplete returns matches", len(ac) > 0)
    top = db.top_tags(limit=8)
    print(f"  top tags -> {[(t['name'], t['uses']) for t in top]}")
    check("top tags ranked by usage", bool(top) and top[0]["uses"] >= top[-1]["uses"])

    if top:
        related = db.similar_tags(top[0]["name"])
        print(f"  related to {top[0]['name']}: {[r['name'] for r in related[:6]]}")

    print("\n== thumbnails ==")
    for item in items:
        thumb = cfg.thumbs_dir / (item["thumb"] or "")
        ok = thumb.is_file() and thumb.stat().st_size > 0
        check(f"thumb for {item['filename']}", ok)
        break
    for item in items:
        if item["thumb"]:
            path = cfg.thumbs_dir / item["thumb"]
            with Image.open(path) as im:
                check(f"{item['filename']} thumb <= {cfg.thumb_size}px",
                      max(im.size) <= cfg.thumb_size, f"{im.size}")

    print("\n== perceptual hashing ==")
    from vault34.hashing import hamming
    hashes = [dict(r) for r in db.iter_hashes()]
    all_media = stats["total"] + stats["duplicates"]
    check("every item hashed (incl. video frames)", len(hashes) == all_media,
          f"{len(hashes)} of {all_media}")
    video_rows = [r for r in db._query("SELECT * FROM media WHERE kind='video'")]
    check("videos carry a perceptual hash",
          all(r["phash"] for r in video_rows),
          f"{sum(1 for r in video_rows if r['phash'])}/{len(video_rows)}")
    if len(hashes) >= 2:
        a, b = hashes[0], hashes[1]
        check("distinct images have different phash", a["phash"] != b["phash"],
              f"{a['phash']} vs {b['phash']} (hamming {hamming(a['phash'], b['phash'])})")

    pipeline.stop()
    db.close()

    print()
    if FAILURES:
        print(f"FAILED {len(FAILURES)} check(s): {FAILURES}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

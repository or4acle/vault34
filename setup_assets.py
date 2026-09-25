"""One-time asset provisioning.

Downloads the WD14 ONNX model and its label vocabulary into ``models/``. This
is the only step that needs network access - once the files are present the
application never contacts anything outside the machine.

    python setup_assets.py            # fetch whatever is missing
    python setup_assets.py --force    # re-download
    python setup_assets.py --check    # report status only
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from vault34.config import load_config

MODEL_URL = "https://huggingface.co/SmilingWolf/wd-v1-4-moat-tagger-v2/resolve/main/model.onnx"
TAGS_URL = "https://huggingface.co/SmilingWolf/wd-v1-4-moat-tagger-v2/resolve/main/selected_tags.csv"

ASSETS = {
    "model": ("wd14.onnx", MODEL_URL),
    "tags": ("selected_tags.csv", TAGS_URL),
}


def human(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def download(url: str, dest: Path) -> None:
    import requests

    dest.parent.mkdir(parents=True, exist_ok=True)
    temp = dest.with_suffix(dest.suffix + ".part")
    print(f"  GET {url}")
    with requests.get(url, stream=True, timeout=120) as response:
        response.raise_for_status()
        total = int(response.headers.get("content-length") or 0)
        done = 0
        last_report = 0.0
        with open(temp, "wb") as handle:
            for chunk in response.iter_content(chunk_size=1 << 20):
                if not chunk:
                    continue
                handle.write(chunk)
                done += len(chunk)
                now = time_now()
                if now - last_report > 1.0:
                    last_report = now
                    if total:
                        pct = 100.0 * done / total
                        print(f"    {human(done)} / {human(total)}  ({pct:5.1f}%)",
                              end="\r", flush=True)
                    else:
                        print(f"    {human(done)}", end="\r", flush=True)
    if total and temp.stat().st_size != total:
        temp.unlink(missing_ok=True)
        raise IOError("download was truncated")
    print(" " * 60, end="\r")
    temp.replace(dest)


def time_now() -> float:
    import time
    return time.monotonic()


def verify(cfg) -> bool:
    """Confirm the model and vocabulary are usable."""
    ok = True
    if not cfg.model_path.is_file():
        print(f"  MISSING model  {cfg.model_path}")
        ok = False
    else:
        print(f"  ok      model  {cfg.model_path.name}  "
              f"{human(cfg.model_path.stat().st_size)}")
    if not cfg.tags_path.is_file():
        print(f"  MISSING tags   {cfg.tags_path}")
        ok = False
    else:
        try:
            from vault34.tagger import load_vocabulary
            vocab = load_vocabulary(cfg.tags_path)
            print(f"  ok      tags   {len(vocab)} labels "
                  f"({len(vocab.rating_indices)} rating, "
                  f"{len(vocab.character_indices)} character)")
        except Exception as exc:  # noqa: BLE001
            print(f"  BAD    tags   {exc}")
            ok = False
    if ok and cfg.model_path.is_file():
        try:
            import onnxruntime as ort
            session = ort.InferenceSession(str(cfg.model_path),
                                           providers=["CPUExecutionProvider"])
            shape = session.get_outputs()[0].shape[-1]
            print(f"  ok      onnx   output {shape} labels, "
                  f"providers={session.get_providers()}")
        except Exception as exc:  # noqa: BLE001
            print(f"  BAD    onnx   {exc}")
            ok = False
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch Vault34 model assets")
    parser.add_argument("--force", action="store_true", help="re-download existing files")
    parser.add_argument("--check", action="store_true", help="verify only, no download")
    args = parser.parse_args()

    cfg = load_config()
    print(f"Vault34 asset setup -> {cfg.models_dir}\n")

    if args.check:
        return 0 if verify(cfg) else 1

    for key, (filename, url) in ASSETS.items():
        dest = cfg.models_dir / filename
        if dest.is_file() and dest.stat().st_size > 0 and not args.force:
            print(f"  have    {filename}  {human(dest.stat().st_size)}")
            continue
        if args.force and dest.is_file():
            print(f"  removing existing {filename}")
            dest.unlink()
        print(f"  fetch   {filename}")
        try:
            download(url, dest)
            print(f"  done    {filename}  {human(dest.stat().st_size)}")
        except Exception as exc:  # noqa: BLE001
            print(f"  FAILED  {filename}: {exc}")
            return 1

    print()
    return 0 if verify(cfg) else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""WD14 (SmilingWolf moat-tagger) inference.

Preprocessing contract for this ONNX export, verified against the graph and
empirically validated:

* input is **NHWC** ``[1, 448, 448, 3]`` (not NCHW),
* values are **raw 0-255** floats - the graph itself starts with
  ``(x - 127.5) * 0.00784314`` (i.e. it applies the standard ``(x-0.5)/0.5``
  normalisation internally), so callers must *not* pre-normalise,
* images are padded to a square with white, then LANCZOS-resized to 448x448,
* the output ``predictions_sigmoid`` is already sigmoid-activated.

The label vocabulary ships separately in ``selected_tags.csv`` whose row order
matches the model's output indices. Categories: ``9`` = rating (indices 0-3),
``0`` = general, ``4`` = character.
"""

from __future__ import annotations

import csv
import threading
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageFile

ImageFile.LOAD_TRUNCATED_IMAGES = True
Image.MAX_IMAGE_PIXELS = None

INPUT_SIZE = 448
CATEGORY_MAP = {"0": "general", "4": "character", "9": "rating", "1": "character",
                "3": "rating"}
RATING_NAMES = ("general", "sensitive", "questionable", "explicit")


class TagVocabulary:
    """The 9083-label WD14 vocabulary with its per-label category."""

    def __init__(self, names: list[str], categories: list[str], rating_indices: list[int]):
        self.names = names
        self.categories = categories
        self.rating_indices = rating_indices
        self.index = {name: i for i, name in enumerate(names)}
        self.general_indices = [i for i, c in enumerate(categories) if c == "general"]
        self.character_indices = [i for i, c in enumerate(categories) if c == "character"]
        self.rating_indices = rating_indices

    def __len__(self) -> int:
        return len(self.names)

    @classmethod
    def from_csv(cls, path: str | Path) -> "TagVocabulary":
        with open(path, newline="", encoding="utf-8") as handle:
            rows = list(csv.reader(handle))
        if not rows:
            raise ValueError(f"{path} is empty")
        header = [h.strip() for h in rows[0]]
        try:
            name_col = header.index("name")
            cat_col = header.index("category")
        except ValueError:
            # positional fallback: tag_id, name, category, count
            name_col, cat_col = 1, 2
        data = rows[1:]
        names = [r[name_col].strip() for r in data]
        categories = [CATEGORY_MAP.get(r[cat_col].strip(), "general") for r in data]
        rating_indices = [i for i, c in enumerate(categories) if c == "rating"]
        if not rating_indices:
            raise ValueError(f"{path} has no rating rows")
        return cls(names, categories, rating_indices)


def load_vocabulary(path: str | Path) -> TagVocabulary:
    return TagVocabulary.from_csv(path)


def preprocess(image: Image.Image, size: int = INPUT_SIZE) -> np.ndarray:
    """Pad to square (white), resize and return a raw 0-255 NHWC batch."""
    image = image.convert("RGB")
    width, height = image.size
    if width != height:
        side = max(width, height)
        canvas = Image.new("RGB", (side, side), (255, 255, 255))
        canvas.paste(image, ((side - width) // 2, (side - height) // 2))
        image = canvas
    if image.size != (size, size):
        image = image.resize((size, size), Image.Resampling.LANCZOS)
    array = np.asarray(image, dtype=np.float32)
    return np.expand_dims(array, 0)


class Tagger:
    """Thread-safe wrapper around an ONNX Runtime WD14 session."""

    def __init__(self, model_path: str | Path, vocabulary: TagVocabulary,
                 providers: list | None = None, threads: int | None = None):
        import onnxruntime as ort

        self.model_path = Path(model_path)
        self.vocab = vocabulary
        self._lock = threading.Lock()
        self.last_error: str | None = None
        self._session = None

        available = ort.get_available_providers()
        preferred = ["CUDAExecutionProvider", "DmlExecutionProvider",
                     "CoreMLExecutionProvider", "CPUExecutionProvider"]
        wanted = [p for p in (providers or preferred) if p in available]
        if "CPUExecutionProvider" not in wanted:
            wanted.append("CPUExecutionProvider")
        if not wanted:
            wanted = ["CPUExecutionProvider"]

        options = ort.SessionOptions()
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        if threads:
            options.intra_op_num_threads = int(threads)

        # Try the fastest provider first, then degrade to CPU-only.
        candidates = [wanted] + [p for p in preferred[::-1] if p in available]
        seen: set[tuple] = set()
        last_error: Exception | None = None
        for provider in candidates:
            key = tuple(provider)
            if key in seen:
                continue
            seen.add(key)
            try:
                self._session = ort.InferenceSession(
                    str(self.model_path), sess_options=options, providers=list(provider))
                self.providers = self._session.get_providers()
                break
            except Exception as exc:  # noqa: BLE001 - fall through to the next attempt
                last_error = exc
                self._session = None
        if self._session is None:
            raise RuntimeError(f"could not load ONNX model: {last_error}")

        self._input = self._session.get_inputs()[0]
        self._output = self._session.get_outputs()[0]
        expected = len(self.vocab)
        shape = self._output.shape[-1]
        if isinstance(shape, int) and shape != expected:
            raise ValueError(
                f"vocabulary/model mismatch: model emits {shape} labels, "
                f"vocabulary has {expected}. Refusing to run.")

    @property
    def ready(self) -> bool:
        return self._session is not None

    def tag_image(self, image: Image.Image, general_threshold: float = 0.35,
                  character_threshold: float = 0.85, max_tags: int = 60) -> dict:
        """Tag a PIL image, returning rating, general tags and characters."""
        batch = preprocess(image, INPUT_SIZE)
        with self._lock:
            probs = self._session.run(
                [self._output.name], {self._input.name: batch})[0][0]
        return self._decode(probs, general_threshold, character_threshold, max_tags)

    def tag_file(self, path: str | Path, general_threshold: float = 0.35,
                 character_threshold: float = 0.85, max_tags: int = 60) -> dict:
        with Image.open(path) as handle:
            handle.seek(0)
            return self.tag_image(handle, general_threshold, character_threshold, max_tags)

    def _decode(self, probs: np.ndarray, general_threshold: float,
                character_threshold: float, max_tags: int) -> dict:
        vocab = self.vocab
        rating_scores = probs[vocab.rating_indices]
        rating_index = vocab.rating_indices[int(np.argmax(rating_scores))]
        rating = vocab.names[rating_index]
        rating_confidence = float(rating_scores.max())

        general: list[tuple[str, float]] = [
            (vocab.names[i], float(probs[i]))
            for i in vocab.general_indices if probs[i] >= general_threshold
        ]
        general.sort(key=lambda kv: kv[1], reverse=True)
        general = general[:max_tags]

        characters: list[tuple[str, float]] = [
            (vocab.names[i], float(probs[i]))
            for i in vocab.character_indices if probs[i] >= character_threshold
        ]
        characters.sort(key=lambda kv: kv[1], reverse=True)
        characters = characters[:max_tags]

        general_map = dict(general)
        for name, score in characters:
            general_map[name] = max(general_map.get(name, 0.0), score)

        return {
            "rating": rating,
            "rating_confidence": rating_confidence,
            "general": general,
            "characters": characters,
            "tags": general_map,
        }

    def warmup(self) -> float:
        """Run one synthetic pass so the first real image is not the slowest."""
        start = time.perf_counter()
        blank = Image.new("RGB", (INPUT_SIZE, INPUT_SIZE), (255, 255, 255))
        self.tag_image(blank)
        return time.perf_counter() - start

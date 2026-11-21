"""Local sentence embeddings (all-MiniLM-L6-v2, 384-d) via fastembed/ONNX - no external API, no per-token
cost, data never leaves the cluster. The model is baked into the image at build time."""
from __future__ import annotations

import hashlib
import math
import os
from functools import lru_cache
from typing import Protocol

DIM = 384
MODEL = "sentence-transformers/all-MiniLM-L6-v2"


class Embedder(Protocol):
    name: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class MiniLM:
    name = MODEL

    def __init__(self) -> None:
        from fastembed import TextEmbedding

        self.model = TextEmbedding(MODEL, cache_dir=os.getenv("FASTEMBED_CACHE", "/opt/models"))

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [v.tolist() for v in self.model.embed(texts)]


class HashEmbedder:
    """Deterministic bag-of-words hashing embedder: used in unit tests (no model download)."""

    name = "hash-384"

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for t in texts:
            v = [0.0] * DIM
            for w in t.lower().split():
                v[int(hashlib.md5(w.encode()).hexdigest(), 16) % DIM] += 1.0
            n = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append([x / n for x in v])
        return out


@lru_cache
def get_embedder() -> Embedder:
    return HashEmbedder() if os.getenv("APP_EMBEDDER") == "hash" else MiniLM()


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=False))  # vectors are L2-normalised

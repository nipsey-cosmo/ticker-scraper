"""Sentence embeddings, absent unless asked for.

Same arrangement as FinBERT in `sentiment_service`: the model is an opt-in
extra, and every caller degrades to "no opinion" when it is not installed
rather than failing. `poetry install --extras semantic`, then set
`STOCKY_EMBED_MODEL`.

Deliberately *not* pgvector, and that is a scope judgement rather than a
shortcut. The only caller embeds cluster winners for one stock — a few dozen
vectors — where a pairwise dot product in Python finishes before a round trip
to Postgres would have left the process. pgvector earns its index at a scale
this will not reach, and it is not available on every Postgres build either.
Nothing is stored: the vectors live for the length of one `prepare()` call and
are thrown away, so there is no column, no migration and nothing to prune.
"""
from __future__ import annotations

import math

import settings

_model = None
_unavailable = False


def available() -> bool:
    """Whether a model is configured *and* importable."""
    return bool(settings.EMBED_MODEL) and not _unavailable


def reset() -> None:
    """Drop the loaded model and the unavailable latch. For tests."""
    global _model, _unavailable
    _model, _unavailable = None, False


def _load():
    """The model, or None once we know there isn't one.

    The latch matters: without it every refresh would pay the import failure
    again, and a missing extra would print a traceback per stock per hour.
    """
    global _model, _unavailable
    if _model is not None or _unavailable:
        return _model
    if not settings.EMBED_MODEL:
        _unavailable = True
        return None
    try:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer(settings.EMBED_MODEL)
        print(f"[embeddings] loaded {settings.EMBED_MODEL}")
    except Exception as exc:
        print(f"[embeddings] {settings.EMBED_MODEL} unavailable "
              f"({type(exc).__name__}), semantic pass is off")
        _unavailable = True
    return _model


def embed(texts: list[str]) -> list[list[float]] | None:
    """Unit-length vectors, one per text, or None when there is no model.

    None and a list of zero vectors are different answers: None means nobody
    had an opinion, so callers skip the pass entirely instead of concluding
    that everything is dissimilar.
    """
    if not texts:
        return []
    model = _load()
    if model is None:
        return None
    try:
        raw = model.encode(texts, normalize_embeddings=True)
    except Exception as exc:
        print(f"[embeddings] encode failed ({type(exc).__name__}), skipping")
        return None
    return [[float(x) for x in vector] for vector in raw]


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity. Vectors from `embed` are already unit length, so this
    is a dot product — the norms are kept for anything that isn't."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0

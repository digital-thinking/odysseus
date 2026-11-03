"""Visual search index for gallery images.

SigLIP 2 (``google/siglip2-base-patch16-224``) embeds images and text queries
into one space; it runs on CPU through fastembed/ONNX (no torch needed) and its
text side understands non-English queries. Vectors are stored in ChromaDB like
the memory and RAG indexes, keyed by gallery image id.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from typing import Callable, Iterable, Optional

from src.constants import FASTEMBED_CACHE_DIR

logger = logging.getLogger(__name__)

MODEL = os.getenv("ODYSSEUS_GALLERY_EMBED_MODEL", "google/siglip2-base-patch16-224")
_BATCH = 16
_GET_CHUNK = 500

_load_lock = threading.Lock()
_image_model = None
_text_model = None
# Background warm-up of the query model: the first load (and, on a fresh
# install, its ~1 GB download) must not happen inside a search request.
_warm_lock = threading.Lock()
_text_ready = threading.Event()
_text_warming = False
_text_error: Optional[str] = None
_SEARCH_WAIT_SECONDS = 20


class SmartSearchUnavailable(RuntimeError):
    """The embedding model or the vector store cannot be used right now."""


def collection_name() -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", MODEL.lower()).strip("_")
    return f"odysseus_gallery_{slug}"[:63].rstrip("_")


def _model_kwargs() -> dict:
    kwargs = {"model_name": MODEL, "cache_dir": FASTEMBED_CACHE_DIR}
    threads = os.getenv("FASTEMBED_THREADS", "").strip()
    if threads.isdigit():
        kwargs["threads"] = int(threads)
    return kwargs


def _image_embedder():
    global _image_model
    with _load_lock:
        if _image_model is None:
            from fastembed import ImageEmbedding
            os.makedirs(FASTEMBED_CACHE_DIR, exist_ok=True)
            _image_model = ImageEmbedding(**_model_kwargs())
        return _image_model


def _text_embedder():
    global _text_model
    with _load_lock:
        if _text_model is None:
            from fastembed import TextEmbedding
            os.makedirs(FASTEMBED_CACHE_DIR, exist_ok=True)
            _text_model = TextEmbedding(**_model_kwargs())
        return _text_model


def _warm_text_model() -> None:
    global _text_warming, _text_error
    try:
        _text_embedder()
        _text_error = None
        _text_ready.set()
    except Exception as exc:
        _text_error = str(exc) or type(exc).__name__
        logger.warning("Smart search: loading the query model failed: %s", exc)
    finally:
        with _warm_lock:
            _text_warming = False


def warm_up(wait_seconds: float = 0) -> bool:
    """Load the query model in the background; returns True once it is ready.

    ``wait_seconds`` blocks up to that long for the load to finish.
    """
    global _text_warming
    if _text_ready.is_set():
        return True
    with _warm_lock:
        if not _text_warming:
            _text_warming = True
            threading.Thread(target=_warm_text_model, daemon=True, name="gallery-smart-search-warmup").start()
    return _text_ready.wait(wait_seconds) if wait_seconds else _text_ready.is_set()


def embed_image_files(paths: list[str]) -> list[list[float]]:
    return [vector.tolist() for vector in _image_embedder().embed(paths, batch_size=_BATCH)]


def embed_query(text: str) -> list[float]:
    return next(iter(_text_embedder().embed([text]))).tolist()


def _collection():
    try:
        from src.chroma_client import get_chroma_client
        client = get_chroma_client()
        return client.get_or_create_collection(name=collection_name(), metadata={"hnsw:space": "cosine"})
    except Exception as exc:
        raise SmartSearchUnavailable(f"Vector store unavailable: {exc}") from exc


def _chunks(items: list, size: int) -> Iterable[list]:
    for start in range(0, len(items), size):
        yield items[start:start + size]


def missing_ids(ids: list[str]) -> list[str]:
    """Ids without a stored vector (so rescans only embed new or changed photos)."""
    if not ids:
        return []
    col = _collection()
    have: set[str] = set()
    for chunk in _chunks(ids, _GET_CHUNK):
        have.update(col.get(ids=chunk, include=[]).get("ids") or [])
    return [i for i in ids if i not in have]


def index_images(
    items: list[tuple[str, str, str, str]],
    *,
    progress: Optional[Callable[[int, int], None]] = None,
    cancel: Optional[threading.Event] = None,
) -> int:
    """Embed and store ``(image_id, thumbnail_path, owner, source_id)`` items. Returns the count stored."""
    if not items:
        return 0
    col = _collection()
    done = 0
    for chunk in _chunks(items, _BATCH * 4):
        if cancel is not None and cancel.is_set():
            break
        vectors = embed_image_files([path for _, path, _, _ in chunk])
        col.upsert(
            ids=[image_id for image_id, _, _, _ in chunk],
            embeddings=vectors,
            metadatas=[{"owner": owner or "", "source_id": source_id or ""} for _, _, owner, source_id in chunk],
        )
        done += len(chunk)
        if progress:
            progress(done, len(items))
    return done


def search(owner: Optional[str], query: str, k: int = 200) -> list[tuple[str, float]]:
    """Best-matching image ids for a text query, as ``(id, similarity)``, most similar first."""
    query = (query or "").strip()
    if not query:
        return []
    if not warm_up(wait_seconds=_SEARCH_WAIT_SECONDS):
        if _text_error and not _text_warming:
            raise SmartSearchUnavailable(f"could not load the search model ({_text_error})")
        raise SmartSearchUnavailable("the search model is still loading — try again in a few seconds")
    try:
        vector = embed_query(query)
    except Exception as exc:
        raise SmartSearchUnavailable(f"Embedding model unavailable: {exc}") from exc
    col = _collection()
    total = col.count()
    if not total:
        return []
    kwargs = {"query_embeddings": [vector], "n_results": min(k, total), "include": ["distances"]}
    if owner is not None:
        kwargs["where"] = {"owner": owner}
    result = col.query(**kwargs)
    ids = (result.get("ids") or [[]])[0]
    distances = (result.get("distances") or [[]])[0]
    return [(image_id, 1.0 - float(dist)) for image_id, dist in zip(ids, distances)]


class NotIndexed(LookupError):
    """The photo has no vector yet (not indexed for smart search)."""


def similar(owner: Optional[str], image_id: str, k: int = 200) -> list[tuple[str, float]]:
    """Photos that look most like ``image_id``, as ``(id, similarity)``, most similar first.

    Uses the photo's stored vector as the query, so no model has to be loaded:
    Chroma answers from its HNSW (approximate nearest neighbour) index. The
    photo itself is left out.
    """
    col = _collection()
    got = col.get(ids=[image_id], include=["embeddings"])
    vectors = got.get("embeddings")
    if vectors is None or len(vectors) == 0:
        raise NotIndexed(image_id)
    total = col.count()
    kwargs = {"query_embeddings": [[float(x) for x in vectors[0]]],
              "n_results": max(1, min(k + 1, total)), "include": ["distances"]}
    if owner is not None:
        kwargs["where"] = {"owner": owner}
    result = col.query(**kwargs)
    ids = (result.get("ids") or [[]])[0]
    distances = (result.get("distances") or [[]])[0]
    return [(i, 1.0 - float(d)) for i, d in zip(ids, distances) if i != image_id][:k]


def delete_ids(ids: list[str]) -> None:
    if not ids:
        return
    try:
        col = _collection()
        for chunk in _chunks(ids, _GET_CHUNK):
            col.delete(ids=chunk)
    except Exception as exc:
        logger.warning("Could not remove %d gallery vectors: %s", len(ids), exc)

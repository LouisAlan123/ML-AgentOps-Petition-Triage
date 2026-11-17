"""Embedding backends behind one interface.
 - sentence-transformers (local, free) when EMBEDDING_BACKEND=sentence-transformers and EMBEDDING_MODEL is set
 - hashing: stateless hashed bag of stemmed uni+bigrams. Needs no download, so a clean clone works offline.
   Because it is stateless, a single chunk can be re-embedded without touching any other vector (L1)."""
from __future__ import annotations
import math
import zlib
import numpy as np
from langchain_core.embeddings import Embeddings
from app.textutil import tokens


class HashingEmbedder:
    def __init__(self, dims: int = 2048):
        self.dims = dims
        self.model_id = f"hashing-{dims}-v1"

    def _vec(self, text: str) -> np.ndarray:
        toks = tokens(text)
        feats = toks + [a + "_" + b for a, b in zip(toks, toks[1:])]
        v = np.zeros(self.dims, dtype="float32")
        counts: dict[int, int] = {}
        for f in feats:
            h = zlib.crc32(f.encode()) % self.dims
            counts[h] = counts.get(h, 0) + 1
        for h, c in counts.items():
            v[h] = 1.0 + math.log(c)
        n = np.linalg.norm(v)
        return v / n if n else v

    def embed(self, texts: list[str]) -> np.ndarray:
        return np.stack([self._vec(t) for t in texts]) if texts else np.zeros((0, self.dims), "float32")


class STEmbedder:
    def __init__(self, model_name: str):
        from sentence_transformers import SentenceTransformer  # lazy: heavy optional dependency
        self.m = SentenceTransformer(model_name, device="cpu")
        self.dims = self.m.get_sentence_embedding_dimension()
        self.model_id = f"st:{model_name}"

    def embed(self, texts: list[str]) -> np.ndarray:
        return np.asarray(self.m.encode(texts, normalize_embeddings=True, show_progress_bar=False), dtype="float32")


def make_embedder(backend: str, model: str = ""):
    if backend == "sentence-transformers":
        if not model:
            raise RuntimeError("EMBEDDING_MODEL must be set when EMBEDDING_BACKEND=sentence-transformers")
        return STEmbedder(model)
    if backend == "hashing":
        return HashingEmbedder()
    raise RuntimeError(f"unknown EMBEDDING_BACKEND {backend}")


class LCEmbeddings(Embeddings):
    """LangChain adapter so FAISS (langchain_community) can embed queries."""
    def __init__(self, inner):
        self.inner = inner
    def embed_documents(self, texts): return self.inner.embed(list(texts)).tolist()
    def embed_query(self, text): return self.inner.embed([text])[0].tolist()

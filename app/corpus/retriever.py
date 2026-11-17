"""Read-only retrieval layer used by the researcher agent's tools.
Dense (LangChain FAISS) + sparse (BM25) with reciprocal-rank fusion, exact clause lookup,
reverse cross-reference lookup and topic listing. Query embeddings are LRU-cached (D7)."""
from __future__ import annotations
import json, math, os, re, threading, warnings
from collections import Counter, OrderedDict
warnings.filterwarnings("ignore", category=DeprecationWarning)
from langchain_community.vectorstores import FAISS
from langchain_community.vectorstores.utils import DistanceStrategy
from app.corpus.embedder import make_embedder, LCEmbeddings
from app.textutil import tokens


class BM25:
    def __init__(self, docs: dict[str, list[str]], k1=1.4, b=0.75):
        self.ids = list(docs)
        self.tf = {i: Counter(t) for i, t in docs.items()}
        self.len = {i: len(t) for i, t in docs.items()}
        self.avg = sum(self.len.values()) / max(len(self.len), 1)
        df = Counter(w for t in docs.values() for w in set(t))
        n = len(docs)
        self.idf = {w: math.log(1 + (n - c + 0.5) / (c + 0.5)) for w, c in df.items()}
        self.k1, self.b = k1, b

    def score(self, q: list[str]) -> list[tuple[str, float]]:
        out = []
        for i in self.ids:
            s, tf, L = 0.0, self.tf[i], self.len[i]
            for w in q:
                if w in tf:
                    f = tf[w]
                    s += self.idf[w] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * L / self.avg))
            if s > 0:
                out.append((i, s))
        return sorted(out, key=lambda x: -x[1])


class Retriever:
    def __init__(self, index_dir: str, backend: str = "hashing", model: str = ""):
        self.meta = json.load(open(os.path.join(index_dir, "index_meta.json")))
        self.chunks = {j["chunk_id"]: j for j in (json.loads(l) for l in open(os.path.join(index_dir, "chunks.jsonl")))}
        self.emb = make_embedder(backend, model)
        if self.emb.model_id != self.meta["embedding_model"]:
            raise RuntimeError(f"index built with {self.meta['embedding_model']} but runtime embedder is {self.emb.model_id}; re-run the pipeline")
        self.vs = FAISS.load_local(os.path.join(index_dir, "faiss"), LCEmbeddings(self.emb),
                                   allow_dangerous_deserialization=True, distance_strategy=DistanceStrategy.MAX_INNER_PRODUCT)
        self.bm25 = BM25({i: tokens(c["heading"] + " " + c["text"] + " " + c["chapter_title"]) for i, c in self.chunks.items()})
        self.cited_by: dict[str, list[str]] = {}
        for i, c in self.chunks.items():
            for r in c["refs"]:
                self.cited_by.setdefault(r, []).append(i)
        self._qcache: OrderedDict[str, list] = OrderedDict()
        self._lock = threading.Lock()
        self.cache_hits = self.cache_misses = 0

    @property
    def corpus_version(self) -> str:
        return self.meta["corpus_version"]

    def resolve_id(self, ref: str) -> str | None:
        ref = ref.strip()
        if ref in self.chunks:
            return ref
        m = re.search(r"(REG|BYE)?[\s\-:§]*(\d{1,2}\.\d{1,2})", ref.upper())
        if not m:
            return None
        for d in ([m.group(1)] if m.group(1) else ["REG", "BYE"]):
            cid = f"{d}-{m.group(2)}"
            if cid in self.chunks:
                return cid
        return None

    def _qvec(self, q: str):
        with self._lock:
            if q in self._qcache:
                self._qcache.move_to_end(q); self.cache_hits += 1
                return self._qcache[q]
        v = self.emb.embed([q])[0].tolist()
        with self._lock:
            self.cache_misses += 1
            self._qcache[q] = v
            if len(self._qcache) > 2048:
                self._qcache.popitem(last=False)
        return v

    def _ok(self, cid, doc, topic):
        c = self.chunks[cid]
        return (not doc or c["doc"] == doc) and (not topic or topic in c["topics"])

    def search(self, query: str, mode: str = "hybrid", top_k: int = 4, doc: str | None = None, topic: str | None = None) -> list[dict]:
        dense, sparse = [], []
        if mode in ("semantic", "hybrid"):
            res = self.vs.similarity_search_with_score_by_vector(self._qvec(query), k=len(self.chunks))
            dense = [(d.metadata["chunk_id"], float(s)) for d, s in res if self._ok(d.metadata["chunk_id"], doc, topic)]
        if mode in ("keyword", "hybrid"):
            sparse = [(i, s) for i, s in self.bm25.score(tokens(query)) if self._ok(i, doc, topic)]
        if mode == "semantic":
            ranked = dense
        elif mode == "keyword":
            ranked = sparse
        else:  # reciprocal rank fusion
            f: dict[str, float] = {}
            for lst in (dense, sparse):
                for r, (i, _) in enumerate(lst):
                    f[i] = f.get(i, 0) + 1 / (60 + r + 1)
            ranked = sorted(f.items(), key=lambda x: -x[1])
        return [self.passage(i, score=round(s, 4), rank=r + 1) for r, (i, s) in enumerate(ranked[:top_k])]

    def passage(self, cid: str, **extra) -> dict:
        c = self.chunks[cid]
        return {"clause_id": cid, "doc": c["doc"], "doc_title": c["doc_title"], "chapter": c["chapter"],
                "chapter_title": c["chapter_title"], "heading": c["heading"], "page": c["page"],
                "topics": c["topics"], "refs": c["refs"], "text": c["text"], **extra}

    def get(self, ref: str) -> dict | None:
        cid = self.resolve_id(ref)
        return self.passage(cid) if cid else None

    def citing(self, ref: str) -> list[dict]:
        cid = self.resolve_id(ref)
        return [self.passage(i) for i in self.cited_by.get(cid, [])] if cid else []

    def list_topic(self, topic: str) -> list[dict]:
        return [{"clause_id": i, "heading": c["heading"], "chapter": c["chapter"]} for i, c in self.chunks.items() if topic in c["topics"]]

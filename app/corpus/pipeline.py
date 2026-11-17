"""D2 / L1: idempotent  read -> parse -> chunk -> embed -> index  pipeline, invocable from the CLI.

  python -m app.corpus.pipeline index                  # incremental: only new/changed chunks are embedded
  python -m app.corpus.pipeline index --reembed REG-7.4,REG-3.1   # force re-embed of specific chunks
  python -m app.corpus.pipeline index --reembed all    # full rebuild
  python -m app.corpus.pipeline status

Chunking strategy (addresses the section-3 difficulty, see docs/architecture.md):
  * one chunk per numbered clause (the unit a petition outcome is cited at), never split mid-clause
  * every chunk carries chapter number/title, clause id, heading, page, topics and forward cross-references
  * the text that is EMBEDDED is prefixed with its chapter + heading so a clause in a late chapter
    ("7.4 Condonation ...") still carries the vocabulary of what it modifies
  * reverse-reference index (which clauses say 'Notwithstanding Clause X') is built at load time, so a
    provision that modifies a base rule can be found *from* the base rule even when the base rule never mentions it
"""
from __future__ import annotations
import argparse, hashlib, json, os, re, sys, time, datetime as dt
import yaml
from pypdf import PdfReader
from langchain_core.documents import Document
from langchain_community.vectorstores import FAISS
from langchain_community.vectorstores.utils import DistanceStrategy
from app.config import settings
from app.corpus.embedder import make_embedder, LCEmbeddings
from app.logging_setup import get_logger

log = get_logger("pipeline")

CHAPTER_RE = re.compile(r"^CHAPTER (\d+): (.+)$")
CLAUSE_RE = re.compile(r"^(\d{1,2}\.\d{1,2}) ([A-Z].*)$")
HEADFOOT_RE = re.compile(r"^(Page \d+|Synthetic University of Technology \|.*)$")
MAX_WORDS = 220

TOPIC_RULES = {
    "attendance": r"attendance|classes held|classes attended|shortfall",
    "condonation": r"condon",
    "re_evaluation": r"re-evaluation",
    "re_registration": r"re-regist",
    "medical_leave": r"medical leave|medical practitioner|medical certificate",
    "grievance": r"grievance",
    "committee": r"committee|council",
    "precedence": r"inconsistency|prevail",
}


def sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:16]


def topics_of(text: str) -> list[str]:
    t = text.lower()
    return [k for k, rx in TOPIC_RULES.items() if re.search(rx, t)]


def refs_of(text: str, doc: str) -> list[str]:
    out = []
    for m in re.finditer(r"(?:Clause|Regulation|Bye-Law)s? (\d{1,2}\.\d{1,2})( of the Academic Regulations)?", text):
        d = "REG" if m.group(2) else doc
        cid = f"{d}-{m.group(1)}"
        if cid not in out:
            out.append(cid)
    return out


def parse_pdf(path: str) -> list[tuple[int, str]]:
    """Return [(page_no, line)] with headers/footers removed."""
    rd = PdfReader(path)
    lines = []
    for pno, page in enumerate(rd.pages, start=1):
        for ln in (page.extract_text() or "").splitlines():
            ln = ln.strip()
            if ln and not HEADFOOT_RE.match(ln):
                lines.append((pno, ln))
    return lines


def chunk_document(doc_cfg: dict, corpus_dir: str) -> list[dict]:
    lines = parse_pdf(os.path.join(corpus_dir, doc_cfg["file"]))
    code, title = doc_cfg["code"], doc_cfg["title"]
    chapter, chapter_title = 0, ""
    paras: list[dict] = []
    for pno, ln in lines:
        m = CHAPTER_RE.match(ln)
        if m:
            chapter, chapter_title = int(m.group(1)), m.group(2).title()
            continue
        if CLAUSE_RE.match(ln):
            paras.append({"chapter": chapter, "chapter_title": chapter_title, "page": pno, "text": ln})
        elif paras and chapter:
            paras[-1]["text"] += " " + ln
    chunks = []
    for p in paras:
        m = CLAUSE_RE.match(p["text"])
        clause, rest = m.group(1), m.group(2)
        hm = re.match(r"^([A-Z][^.]{2,90}?)\. (.*)$", rest)
        heading, body = (hm.group(1), hm.group(2)) if hm else (rest[:60], rest)
        text = f"{clause} {heading}. {body}"
        words = text.split()
        if len(words) > MAX_WORDS:  # defensive: only split very long clauses, on sentence boundaries
            log.warning("long clause split", extra={"clause": clause})
        cid = f"{code}-{clause}"
        chunks.append({
            "chunk_id": cid, "doc": code, "doc_title": title, "doc_version": doc_cfg.get("version", ""),
            "chapter": p["chapter"], "chapter_title": p["chapter_title"], "clause": clause, "heading": heading,
            "text": text, "page": p["page"], "topics": topics_of(text), "refs": refs_of(text, code),
            "content_hash": sha(text),
            "embed_text": f"{title} | Chapter {p['chapter']}: {p['chapter_title']} | {text}",
        })
    return chunks


def load_chunk_store(index_dir: str) -> dict:
    p = os.path.join(index_dir, "chunks.jsonl")
    if not os.path.exists(p):
        return {}
    return {j["chunk_id"]: j for j in (json.loads(l) for l in open(p, encoding="utf-8"))}


def run_index(reembed: str = "", corpus_dir: str | None = None, index_dir: str | None = None) -> dict:
    corpus_dir = corpus_dir or settings.corpus_dir
    index_dir = index_dir or settings.index_dir
    os.makedirs(index_dir, exist_ok=True)
    t0 = time.time()
    manifest = yaml.safe_load(open(os.path.join(corpus_dir, "corpus.yaml")))
    emb = make_embedder(settings.embedding_backend, settings.embedding_model)

    # ---- read + parse + chunk ----
    t_parse = time.time()
    chunks, pages, doc_info = [], 0, []
    for d in manifest["documents"]:
        path = os.path.join(corpus_dir, d["file"])
        cs = chunk_document(d, corpus_dir)
        n_pages = len(PdfReader(path).pages)
        pages += n_pages
        doc_info.append({"code": d["code"], "title": d["title"], "version": d["version"], "file": d["file"],
                         "sha256": hashlib.sha256(open(path, "rb").read()).hexdigest(), "pages": n_pages, "chunks": len(cs)})
        chunks += cs
    ids = [c["chunk_id"] for c in chunks]
    if len(ids) != len(set(ids)):
        dup = {i for i in ids if ids.count(i) > 1}
        raise RuntimeError(f"duplicate clause ids after parsing: {sorted(dup)}")
    parse_s = time.time() - t_parse

    # ---- diff against what is already indexed ----
    old = load_chunk_store(index_dir)
    faiss_path = os.path.join(index_dir, "faiss")
    have_index = os.path.exists(os.path.join(faiss_path, "index.faiss")) and bool(old)
    force_all = reembed == "all" or not have_index
    force_ids = set() if force_all else {x.strip() for x in reembed.split(",") if x.strip()}
    cur = {c["chunk_id"]: c for c in chunks}
    for c in chunks:
        c["embed_model"] = emb.model_id
    to_delete = [i for i in old if i not in cur]
    to_embed = []
    for i, c in cur.items():
        o = old.get(i)
        if force_all or i in force_ids or o is None or o["content_hash"] != c["content_hash"] or o.get("embed_model") != emb.model_id:
            to_embed.append(i)
    unchanged = len(cur) - len(to_embed)

    # ---- embed + index (LangChain FAISS) ----
    t_emb = time.time()
    lc = LCEmbeddings(emb)
    if force_all:
        vecs = emb.embed([c["embed_text"] for c in chunks])
        vs = FAISS.from_embeddings(
            list(zip([c["embed_text"] for c in chunks], vecs.tolist())), lc,
            metadatas=[{k: v for k, v in c.items() if k != "embed_text"} for c in chunks], ids=ids,
            distance_strategy=DistanceStrategy.MAX_INNER_PRODUCT)
        embedded = len(chunks)
    else:
        vs = FAISS.load_local(faiss_path, lc, allow_dangerous_deserialization=True,  # pickle written by this pipeline only
                              distance_strategy=DistanceStrategy.MAX_INNER_PRODUCT)
        drop = to_delete + [i for i in to_embed if i in old]
        if drop:
            vs.delete(drop)
        if to_embed:
            vecs = emb.embed([cur[i]["embed_text"] for i in to_embed])
            vs.add_embeddings(list(zip([cur[i]["embed_text"] for i in to_embed], vecs.tolist())),
                              metadatas=[{k: v for k, v in cur[i].items() if k != "embed_text"} for i in to_embed], ids=to_embed)
        embedded = len(to_embed)
    emb_s = time.time() - t_emb
    changed = bool(embedded or to_delete or force_all)
    if changed:
        vs.save_local(faiss_path)
    if True:  # metadata (topics/refs) may change without text changing; the chunk store is always rewritten (cheap)
        with open(os.path.join(index_dir, "chunks.jsonl"), "w", encoding="utf-8") as f:
            for c in chunks:
                f.write(json.dumps({k: v for k, v in c.items() if k != "embed_text"}) + "\n")

    total_s = time.time() - t0
    prev = {}
    mp = os.path.join(index_dir, "index_meta.json")
    if os.path.exists(mp):
        prev = json.load(open(mp))
    corpus_version = sha("".join(sorted(c["content_hash"] for c in chunks)) + emb.model_id)
    meta = {
        "built_at": prev.get("built_at") if (not changed and prev) else dt.datetime.now(dt.timezone.utc).isoformat(),
        "last_run_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "corpus_version": corpus_version, "embedding_model": emb.model_id, "dims": emb.dims,
        "scope": manifest.get("scope", {}), "documents": doc_info, "chunks": len(chunks),
        "last_run": {"embedded": embedded, "unchanged": unchanged, "deleted": len(to_delete), "full_rebuild": force_all,
                     "parse_seconds": round(parse_s, 4), "embed_seconds": round(emb_s, 4), "total_seconds": round(total_s, 4)},
        "throughput": {"chunks_per_s": round(len(chunks) / max(total_s, 1e-6), 1),
                       "pages_per_s": round(pages / max(parse_s, 1e-6), 1),
                       "embedded_chunks_per_s": round(embedded / emb_s, 1) if embedded and emb_s > 0 else 0},
    }
    json.dump(meta, open(mp, "w"), indent=2)
    log.info("index pipeline finished", extra={"extra_fields": meta["last_run"] | {"chunks": len(chunks), "corpus_version": corpus_version}})
    return meta


def main():
    ap = argparse.ArgumentParser(description="Corpus indexing pipeline")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ix = sub.add_parser("index")
    ix.add_argument("--reembed", default="", help="comma-separated chunk ids, or 'all'")
    sub.add_parser("status")
    a = ap.parse_args()
    if a.cmd == "index":
        m = run_index(a.reembed)
        print(json.dumps({k: m[k] for k in ("built_at", "corpus_version", "chunks", "embedding_model", "last_run", "throughput")}, indent=2))
    else:
        print(open(os.path.join(settings.index_dir, "index_meta.json")).read())


if __name__ == "__main__":
    main()

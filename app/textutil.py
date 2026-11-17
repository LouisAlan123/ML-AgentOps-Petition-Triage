"""Small, dependency-free text helpers shared by indexer, retriever, verifier and eval."""
from __future__ import annotations
import re
import unicodedata

STOP = set("""a an the and or of to in on for with by at from as is are was were be been being this that these those it its
their his her they them we you i my our your shall may must will would should can could not no any all each every such
which who whom whose than then there here has have had do does did if else when where while into upon per under over about
also only other another more most less least so up out off""".split())
# words that carry the *routing* meaning rather than domain content; ignored by the groundedness metric
ROUTING_WORDS = set("""refer referred refers route routed routing committee grant granted reject rejected eligible eligibility petition
petitions student students decision decide decided escalate escalated human officer examination position application applications
recommend recommended cited clause clauses regulation regulations provision provisions therefore however hence thus since because
accordingly basis case under per""".split())

_TOK = re.compile(r"[a-z0-9]+(?:[.\-][a-z0-9]+)*%?")


def normalize(text: str) -> str:
    t = unicodedata.normalize("NFKC", text)
    t = "".join(ch for ch in t if unicodedata.category(ch) not in ("Cf",))  # strip zero-width etc.
    return re.sub(r"\s+", " ", t).strip()


def stem(w: str) -> str:
    if len(w) > 7 and w.endswith("ations"): return w[:-6]
    if len(w) > 6 and w.endswith("ation"): return w[:-5]
    if len(w) > 5 and w.endswith("ing"): return w[:-3]
    if len(w) > 5 and w.endswith("ed"): return w[:-2]
    if len(w) > 4 and w.endswith("es"): return w[:-2]
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"): return w[:-1]
    if len(w) > 4 and w.endswith("e"): return w[:-1]
    return w


def tokens(text: str, keep_stop: bool = False, do_stem: bool = True) -> list[str]:
    out = []
    for m in _TOK.findall(normalize(text).lower()):
        if not keep_stop and m in STOP:
            continue
        out.append(stem(m) if do_stem and not m[0].isdigit() else m)
    return out


def sentences(text: str) -> list[str]:
    t = normalize(text)
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9(])", t)
    return [p.strip() for p in parts if p.strip()]


def numbers(text: str) -> set[str]:
    """Numeric literals with optional %, e.g. '75%', '65', '7'. Clause ids like 3.1 are returned as '3.1'."""
    return set(re.findall(r"\d+(?:\.\d+)?%?", normalize(text)))

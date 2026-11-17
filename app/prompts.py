"""Prompts are versioned artifacts (L2): prompts/<name>.<version>.txt. Every LLM step records name, version and
content hash, so any response can be traced to the exact prompt text that produced it."""
from __future__ import annotations
import glob, hashlib, json, os
from string import Template

PROMPT_DIR = os.environ.get("PROMPT_DIR", os.path.join(os.path.dirname(os.path.dirname(__file__)), "prompts"))
_cache: dict = {}


def load(name: str, version: str) -> tuple[str, str]:
    key = (name, version)
    if key not in _cache:
        txt = open(os.path.join(PROMPT_DIR, f"{name}.{version}.txt"), encoding="utf-8").read()
        _cache[key] = (txt, hashlib.sha256(txt.encode()).hexdigest()[:12])
    return _cache[key]


def render(name: str, version: str, **vars) -> tuple[str, str]:
    txt, h = load(name, version)
    vals = {k: (v if isinstance(v, str) else json.dumps(v, default=str)) for k, v in vars.items()}
    return Template(txt).safe_substitute(vals), h


def manifest() -> list[dict]:
    out = []
    for p in sorted(glob.glob(os.path.join(PROMPT_DIR, "*.txt"))):
        name, version = os.path.basename(p)[:-4].rsplit(".", 1)
        out.append({"name": name, "version": version, "sha": load(name, version)[1]})
    return out

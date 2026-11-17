"""L4 guardrails: prompt-injection detection on every free-text field, toxicity detection on input and output,
spotlighting of untrusted text inside prompts, tool-call allow-listing, and an output filter that stops the
system issuing a determination it is not authorised to make (only the examination officer decides)."""
from __future__ import annotations
import re
import secrets
from dataclasses import dataclass, field
from app.textutil import normalize

INJECTION_PATTERNS = [  # (regex, weight)
    (r"\b(ignore|disregard|forget|override)\b.{0,40}\b(previous|prior|above|earlier|all|any|the)\b.{0,30}\b(instruction|rule|prompt|polic|guideline)", 0.9),
    (r"\b(you are now|from now on you|act as (an?|the) (admin|system|developer|officer|controller)|pretend to be)\b", 0.7),
    (r"\b(system|developer)\s*(prompt|message|instruction)", 0.6),
    (r"\b(reveal|print|show|repeat)\b.{0,20}\b(your|the)\b.{0,10}\b(prompt|instructions|system)", 0.7),
    (r"\b(approve|grant|accept)\b.{0,25}\b(this|my) petition\b.{0,30}\b(immediately|automatically|without|regardless)", 0.6),
    (r"\b(set|mark|change|return|output)\b.{0,20}\b(route|decision|status|eligibility)\b.{0,20}\b(to|as|=)\b", 0.7),
    (r"</?\s*(system|assistant|user|tool)\s*>|\[/?(inst|system)\]|<\|im_(start|end)\|>", 0.9),
    (r"\b(jailbreak|dan mode|do anything now)\b", 0.9),
    (r"\b(call|invoke|use)\b.{0,15}\b(tool|function)\b", 0.4),
    (r"\bbase64\b|\\x[0-9a-f]{2}", 0.3),
]
TOXIC_SEVERE = [r"\b(i will|i'll|gonna) (kill|hurt|attack|burn)\b", r"\b(kill yourself|kys)\b", r"\bbomb (the|your)\b"]
TOXIC_MILD = [r"\b(idiot|idiots|stupid|moron|useless|bastard|shut up|damn you|screw you|incompetent fools?)\b"]
INJECTION_BLOCK_THRESHOLD = 0.6


@dataclass
class GuardResult:
    injection_score: float = 0.0
    injection_hits: list[str] = field(default_factory=list)
    toxicity: str = "none"          # none | mild | severe
    toxicity_hits: list[str] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return self.injection_score >= INJECTION_BLOCK_THRESHOLD or self.toxicity == "severe"

    @property
    def flags(self) -> list[str]:
        f = []
        if self.injection_score >= INJECTION_BLOCK_THRESHOLD: f.append("prompt_injection")
        elif self.injection_score > 0: f.append("prompt_injection_suspected")
        if self.toxicity != "none": f.append(f"toxicity_{self.toxicity}")
        return f


def scan_text(text: str) -> GuardResult:
    t = normalize(text).lower()
    g = GuardResult()
    score = 0.0
    for rx, w in INJECTION_PATTERNS:
        if re.search(rx, t):
            g.injection_hits.append(rx[:40])
            score = 1 - (1 - score) * (1 - w)   # noisy-or combination
    g.injection_score = round(score, 3)
    for rx in TOXIC_SEVERE:
        if re.search(rx, t):
            g.toxicity, g.toxicity_hits = "severe", g.toxicity_hits + [rx]
    if g.toxicity == "none":
        for rx in TOXIC_MILD:
            if re.search(rx, t):
                g.toxicity, g.toxicity_hits = "mild", g.toxicity_hits + [rx]
    return g


def scan_fields(obj: dict, free_text_fields=("description", "client_petition_ref")) -> GuardResult:
    """Every free-text field that can reach a model is scanned; results are merged."""
    merged = GuardResult()
    for f in free_text_fields:
        v = obj.get(f)
        if isinstance(v, str) and v:
            g = scan_text(v)
            merged.injection_score = max(merged.injection_score, g.injection_score)
            merged.injection_hits += g.injection_hits
            if g.toxicity == "severe" or (g.toxicity == "mild" and merged.toxicity == "none"):
                merged.toxicity = g.toxicity
            merged.toxicity_hits += g.toxicity_hits
    return merged


def spotlight(text: str) -> str:
    """Wrap untrusted text in a random-nonce delimiter so the model can tell data from instructions."""
    nonce = secrets.token_hex(4)
    clean = re.sub(r"<<\s*/?\s*UNTRUSTED[^>]*>>", "", text)
    return f"<<UNTRUSTED_{nonce}>>\n{clean}\n<</UNTRUSTED_{nonce}>>"


ALLOWED_TOOLS = {"search_corpus", "get_clause", "get_petition_fields", "find_citing_clauses", "list_clauses_by_topic"}


def validate_tool_call(name: str, args: dict) -> tuple[bool, str]:
    if name not in ALLOWED_TOOLS:
        return False, f"tool {name!r} not allowed"
    if not isinstance(args, dict):
        return False, "args must be an object"
    for k, v in args.items():
        if isinstance(v, str) and len(v) > 400:
            return False, f"argument {k} too long"
    if name == "search_corpus":
        if not isinstance(args.get("query"), str) or not args["query"].strip():
            return False, "query required"
        if args.get("mode", "hybrid") not in ("semantic", "keyword", "hybrid"):
            return False, "bad mode"
        if not (1 <= int(args.get("top_k", 4)) <= 8):
            return False, "top_k out of range"
    if name in ("get_clause", "find_citing_clauses") and not re.fullmatch(r"(REG|BYE)?-?\s*\d{1,2}\.\d{1,2}", str(args.get("clause_id", "")).strip(), re.I):
        return False, "clause_id must look like REG-7.4"
    return True, ""


ALLOWED_ROUTES = {"grant-under-provision", "refer-to-committee", "reject-with-cited-clause", "escalate-to-human"}
DETERMINATION_PATTERNS = [
    r"\b(petition|application|request) (is|has been|stands) (hereby )?(approved|granted|accepted|rejected|allowed|sanctioned)\b",
    r"\bhereby (approve|grant|reject|condone|sanction|allow)\b",
    r"\b(final decision|decision is final|we have (approved|granted|rejected|condoned))\b",
    r"\b(shortfall|attendance) (is|has been) (hereby )?condoned\b",
    r"\byou (are|have been) (approved|granted|permitted to write)\b",
]


def filter_output(draft: dict) -> list[str]:
    """Return violations. Any violation => the draft must not be returned (caller escalates)."""
    v = []
    if draft.get("route") not in ALLOWED_ROUTES:
        v.append(f"route {draft.get('route')!r} not permitted")
    text = " ".join(str(draft.get(k, "")) for k in ("basis", "rationale"))
    for rx in DETERMINATION_PATTERNS:
        if re.search(rx, text, re.I):
            v.append("unauthorised determination language: " + re.search(rx, text, re.I).group(0))
    g = scan_text(text)
    if g.toxicity != "none":
        v.append(f"toxic output ({g.toxicity})")
    if g.injection_score >= INJECTION_BLOCK_THRESHOLD:
        v.append("output contains instruction-like content")
    return v

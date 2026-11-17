"""Deterministic detection of conflicting provisions (F7): two retrieved provisions give different time limits for
the same event, and the petition's own fact falls between them, so the answer depends on which one prevails."""
from __future__ import annotations
import re

SUBJECTS = [
    # (name, applies-to petition types, subject regex, window regex, petition field)
    ("re-evaluation time limit", {"re_evaluation"}, r"re-evaluation", r"within (\d+) days of (?:the )?declaration of results", "days_since_result"),
    ("medical certificate time limit", {"attendance"}, r"certificate", r"within (\d+) days of the student resuming classes", "days_since_resumption"),
    ("grievance time limit", {"grade_grievance"}, r"grievance", r"within (\d+) days of (?:the )?declaration of results", "days_since_result"),
]


def detect(petition: dict, ptype: str, passages: list[dict]) -> dict | None:
    for name, types, subj, win, field in SUBJECTS:
        if ptype not in types:
            continue
        if name.startswith("medical") and petition.get("grounds") != "medical":
            continue
        wins = []
        for p in passages:
            if re.search(subj, p["text"], re.I):
                m = re.search(win, p["text"])
                if m:
                    wins.append((int(m.group(1)), p["clause_id"]))
        vals = sorted({w for w, _ in wins})
        d = petition.get(field)
        if len(vals) > 1 and d is not None and vals[0] < d <= vals[-1]:
            prec = next((p["clause_id"] for p in passages if "inconsistency" in p["text"]), None)
            return {"subject": name, "field": field, "petition_value": d,
                    "provisions": [{"clause_id": c, "limit_days": w} for w, c in wins], "precedence_clause": prec}
    return None

"""Clause readers + the reasoning policy of the deterministic offline provider ("heuristic").

IMPORTANT (be honest in the viva): this file is a STAND-IN FOR THE LLM. It lets the whole system run, be tested and be
load-tested with no API key and no rate limits. It only sees what the agents put in the prompt payload (retrieved
passages + validated fields), exactly like a real model would. It reads thresholds, bands, windows and committee names
out of the passage TEXT; it does not hard-code clause numbers. Swap GATEWAY_PROVIDER=openai_compat for a real model."""
from __future__ import annotations
import re
from app.textutil import sentences, tokens

WORDNUM = {"once": 1, "twice": 2, "thrice": 3, "one": 1, "two": 2, "three": 3}


def _t(p): return p["text"]


def find(passages, pred):
    for p in passages:
        try:
            v = pred(p)
        except Exception:
            v = None
        if v:
            return p, v
    return None, None


def quote(p, rx: str | None = None) -> str:
    ss = sentences(p["text"])
    if rx:
        for s in ss:
            if re.search(rx, s, re.I):
                return s
    return ss[0] if ss else p["text"]


# ---------------- clause readers (return parameter or None) ----------------
def r_min_attendance(p):
    m = re.search(r"attended not less than (\d+(?:\.\d+)?)% of the classes held", _t(p))
    return float(m.group(1)) if m else None

def r_condonation_band(p):
    if "condon" not in _t(p).lower(): return None
    m = re.search(r"not less than (\d+)% but less than (\d+)%", _t(p))
    return (float(m.group(1)), float(m.group(2))) if m else None

def r_below_band_bar(p):
    m = re.search(r"less than (\d+)% shall not be condoned", _t(p))
    return float(m.group(1)) if m else None

def r_condonation_limit(p):
    m = re.search(r"[Cc]ondonation.*not more than (once|twice|thrice|\d+)", _t(p))
    if not m:
        return None
    g = m.group(1)
    return WORDNUM.get(g) or int(g)

def r_window(p, event_rx):
    m = re.search(r"within (\d+) days of (?:the )?" + event_rx, _t(p))
    return int(m.group(1)) if m else None

def r_duty_cap(p):
    if "counted as classes attended" not in _t(p): return None
    m = re.search(r"maximum of (\d+)% of the classes held", _t(p))
    return float(m.group(1)) if m else None

COMMITTEE_RX = r"(?:decided|considered|examine[sd]?|addressed|referred)\s+(?:by|to)\s+the\s+((?:[A-Z][a-z]+\s)+(?:Committee|Council))"

def r_committee(p):
    m = re.search(COMMITTEE_RX, _t(p))
    return m.group(1).strip() if m else None

def r_composition(p):
    return "shall consist of" in _t(p)

def r_max_attempts(p):
    m = re.search(r"maximum of (\d+) attempts", _t(p))
    return int(m.group(1)) if m and "re-register" in _t(p) else None

def r_exhausted(p):
    return r_committee(p) if "exhausted" in _t(p) else None

def r_rereg(p):
    return "grade F or DT" in _t(p) and "shall re-register" in _t(p)

def r_no_improvement(p):
    return "shall not re-register" in _t(p) and "improving" in _t(p)

def r_leave_hod(p):
    m = re.search(r"medical leave for a period of up to (\d+) consecutive days", _t(p))
    return int(m.group(1)) if m else None

def r_leave_committee(p):
    m = re.search(r"exceeding (\d+) days and up to (\d+) days", _t(p))
    return (int(m.group(1)), int(m.group(2))) if m and "leave" in _t(p).lower() else None

def r_leave_cap(p):
    m = re.search(r"exceeding (\d+) days in a semester shall not be granted", _t(p))
    return int(m.group(1)) if m else None

def r_reeval_elig(p):
    return "re-evaluation" in _t(p) and "theory course" in _t(p) and "may apply" in _t(p)

def r_reeval_excluded(p):
    m = re.search(r"^[\d.]+ [^.]*\.\s*(.*?) courses are not eligible for re-evaluation", _t(p))
    if not m and "not eligible for re-evaluation" not in _t(p): return None
    return [w.lower() for w in re.findall(r"Laboratory|project|seminar", _t(p), re.I)]

def r_reeval_once(p):
    return "re-evaluation" in _t(p) and "only once" in _t(p)

def r_reeval_window(p):
    return r_window(p, r"declaration of results") if "re-evaluation" in _t(p).lower() else None

def r_grievance_window(p):
    return r_window(p, r"declaration of results") if "grievance" in _t(p).lower() else None

def r_grievance_final(p):
    return "grievance" in _t(p).lower() and "decision shall be final" in _t(p)

def r_grievance_late(p):
    return "grievance" in _t(p).lower() and "shall not be entertained" in _t(p)

def r_precedence(p):
    return "inconsistency" in _t(p)

LAB = {"lab": "laboratory", "project": "project"}

BASE_READER = {"attendance": r_min_attendance, "re_evaluation": r_reeval_elig, "re_registration": r_rereg,
               "medical_leave": r_leave_hod, "grade_grievance": r_grievance_window}


def _cite(p, role, rx=None):
    return {"clause_id": p["clause_id"], "quote": quote(p, rx), "role": role}


def _out(route, position, basis, rationale, decisive: list, supporting: list, style: str, escalation=None):
    cites = decisive if style == "v1" else decisive + [c for c in supporting if c["clause_id"] not in {d["clause_id"] for d in decisive}]
    if style == "v1":
        cites = cites[:1]
    return {"route": route, "eligibility_position": position, "basis": basis, "rationale": rationale,
            "citations": cites, "escalation_reason": escalation}


def decide(f: dict, ptype: str, passages: list[dict], style: str = "v2") -> dict:
    P = passages
    if ptype == "attendance":
        base, T = find(P, r_min_attendance)
        if not base:
            return _out("escalate-to-human", "undetermined", "The minimum attendance provision was not found in the retrieved provisions.",
                        "No retrieved provision states the attendance requirement, so no position can be grounded.", [], [], style, "insufficient_evidence")
        held, att_c = f.get("classes_held"), f.get("classes_attended")
        att = round(100 * att_c / held, 1) if held and att_c is not None else f.get("attendance_percent")
        bq = r"not less than \d+%"
        if att >= T:
            return _out("reject-with-cited-clause", "eligible", f"No shortfall: attendance meets the {T:g}% minimum.",
                        f"Attendance of {att:g}% is not less than the {T:g}% minimum in {base['clause_id']}, so no condonation is needed and the petition is not required.",
                        [_cite(base, "base", bq)], [], style)
        duty, cap = find(P, r_duty_cap)
        if duty and f.get("grounds") == "sports_duty" and f.get("duty_classes") and held:
            credit = min(f["duty_classes"], int(cap / 100 * held))
            eff = round(100 * (att_c + credit) / held, 1)
            if eff >= T:
                return _out("grant-under-provision", "eligible", f"Duty classes counted as attended under {duty['clause_id']} bring attendance to the {T:g}% minimum.",
                            f"Attendance of {att:g}% is below the {T:g}% minimum in {base['clause_id']}. Under {duty['clause_id']}, classes missed while representing the University with prior written approval shall be counted as classes attended, subject to a maximum of {cap:g}% of the classes held; counting {credit} duty classes as attended gives {eff:g}%, not less than the {T:g}% minimum.",
                            [_cite(duty, "modifier", r"counted as classes attended")], [_cite(base, "base", bq)], style)
        cond, band = find(P, r_condonation_band)
        cons, _ = find(P, lambda p: "Consequence" in p["heading"] or ("DT" in p["text"] and "not be permitted" in p["text"]))
        if not cond:
            sup = [_cite(cons, "supporting", r"not be permitted")] if cons else []
            return _out("reject-with-cited-clause", "not_eligible", f"Attendance is below the {T:g}% minimum.",
                        f"Attendance of {att:g}% is below the {T:g}% minimum required by {base['clause_id']}, so the student is not eligible to appear for the ESE in the course.",
                        [_cite(base, "base", bq)], sup, style)
        lo, hi = band
        cq = r"condoned where"
        below, _ = find(P, r_below_band_bar)
        if att < lo:
            dec = below or cond
            return _out("reject-with-cited-clause", "not_eligible", f"Attendance is below {lo:g}%, which cannot be condoned.",
                        f"Attendance of {att:g}% is below the {T:g}% minimum in {base['clause_id']} and below the {lo:g}% floor for condonation in {cond['clause_id']}; a shortfall where attendance is less than {lo:g}% shall not be condoned.",
                        [_cite(dec, "modifier", r"shall not be condoned|less than")], [_cite(base, "base", bq), _cite(cond, "modifier", cq)], style)
        if f.get("grounds") != "medical":
            return _out("reject-with-cited-clause", "not_eligible", "Condonation is available only for certified illness or hospitalisation.",
                        f"Attendance of {att:g}% is below the {T:g}% minimum in {base['clause_id']}. {cond['clause_id']} allows condonation only where the shortfall is due to illness or hospitalisation certified by a registered medical practitioner, and the petition states {f.get('grounds')} grounds.",
                        [_cite(cond, "modifier", cq)], [_cite(base, "base", bq)], style)
        if not f.get("medical_certificate_attached"):
            return _out("reject-with-cited-clause", "not_eligible", "No medical certificate is attached.",
                        f"Attendance of {att:g}% is below the {T:g}% minimum in {base['clause_id']}. Condonation under {cond['clause_id']} requires illness certified by a registered medical practitioner, and no medical certificate is attached.",
                        [_cite(cond, "modifier", cq)], [_cite(base, "base", bq)], style)
        lim, n = find(P, r_condonation_limit)
        if lim and f.get("prior_condonations", 0) >= n:
            return _out("reject-with-cited-clause", "not_eligible", f"Condonation has already been granted {f['prior_condonations']} times.",
                        f"Attendance of {att:g}% is below the {T:g}% minimum in {base['clause_id']}. Under {lim['clause_id']}, condonation under {cond['clause_id']} shall be granted not more than {['', 'once', 'twice', 'thrice'][n] if n < 4 else n} during the programme, and the petition gives {f['prior_condonations']} prior condonations.",
                        [_cite(lim, "modifier", r"not more than")], [_cite(base, "base", bq), _cite(cond, "modifier", cq)], style)
        w = r_window(cond, r"student resuming classes")
        d = f.get("days_since_resumption")
        if w is not None and d is not None and d > w:
            return _out("reject-with-cited-clause", "not_eligible", f"The certificate was not submitted within {w} days of resuming classes.",
                        f"{cond['clause_id']} requires the medical certificate within {w} days of the student resuming classes; the petition states {d} days.",
                        [_cite(cond, "modifier", r"within \d+ days")], [_cite(base, "base", bq)], style)
        comm = r_committee(cond) or "committee"
        comp, _ = find(P, r_composition)
        sup = [_cite(base, "base", bq)] + ([_cite(cons, "base", r"not be permitted")] if cons else []) + ([_cite(lim, "supporting", r"not more than")] if lim else []) + ([_cite(comp, "committee")] if comp else [])
        timing = "" if d is not None else " The petition does not state when the certificate was submitted; the committee should confirm it."
        return _out("refer-to-committee", "conditionally_eligible_pending_committee",
                    f"Shortfall is within the condonable band of {cond['clause_id']}; the decision is reserved to the {comm}.",
                    f"Attendance of {att:g}% is below the {T:g}% minimum in {base['clause_id']}, " + (f"and under {cons['clause_id']} a student who does not satisfy {base['clause_id']} shall not be permitted to appear for the ESE." if cons else "so the student is not eligible to appear for the ESE on that rule alone.") + f" {cond['clause_id']} provides that, notwithstanding {base['clause_id']}, a shortfall may be condoned where attendance is not less than {lo:g}% but less than {hi:g}% and the shortfall is due to illness certified by a registered medical practitioner; the petition states medical grounds and a medical certificate is attached. {cond['clause_id']} reserves each application to the {comm}, so the petition is referred to it.{timing}",
                    [_cite(cond, "modifier", cq), _cite(cond, "committee", r"decided by")] if style != "v1" else [_cite(cond, "modifier", r"decided by")], sup, style, "committee_reserved")

    if ptype == "re_evaluation":
        elig, _ = find(P, r_reeval_elig)
        if not elig:
            return _out("escalate-to-human", "undetermined", "No re-evaluation provision was retrieved.", "The retrieved provisions do not cover re-evaluation.", [], [], style, "insufficient_evidence")
        excl, kinds = find(P, r_reeval_excluded)
        if excl and f.get("course_type") in LAB and LAB[f["course_type"]] in (kinds or []):
            return _out("reject-with-cited-clause", "not_eligible", f"{f['course_type'].title()} courses are not eligible for re-evaluation.",
                        f"The course is a {f['course_type']} course and {excl['clause_id']} states that laboratory, project and seminar courses are not eligible for re-evaluation.",
                        [_cite(excl, "base")], [_cite(elig, "base")], style)
        once, _ = find(P, r_reeval_once)
        if once and f.get("prior_reevaluations", 0) >= 1:
            return _out("reject-with-cited-clause", "not_eligible", "Re-evaluation of a course is permitted only once.",
                        f"{once['clause_id']} permits re-evaluation of a course only once, and the petition states {f['prior_reevaluations']} prior re-evaluation.",
                        [_cite(once, "base")], [_cite(elig, "base")], style)
        wins = [(r_reeval_window(p), p) for p in P if r_reeval_window(p)]
        d = f.get("days_since_result")
        if not wins:
            return _out("escalate-to-human", "undetermined", "No time limit for re-evaluation was retrieved.", "The retrieved provisions state no time limit for re-evaluation.", [], [], style, "insufficient_evidence")
        lo = min(w for w, _ in wins); hi = max(w for w, _ in wins)
        wc = [_cite(p, "base", r"within \d+ days") for _, p in wins]
        if d <= lo:
            return _out("grant-under-provision", "eligible", f"The application is within the {lo}-day time limit for re-evaluation of a theory course.",
                        f"{elig['clause_id']} permits re-evaluation of the ESE answer script of a theory course, and the application is made {d} days after the declaration of results, within the limit of {lo} days.",
                        [_cite(elig, "base")], wc, style)
        if d > hi:
            return _out("reject-with-cited-clause", "not_eligible", f"The application is outside the {hi}-day time limit.",
                        f"The application is made {d} days after the declaration of results, beyond the limit of {lo} days in {wins[0][1]['clause_id']}" + (f" and the limit of {hi} days" if hi != lo else "") + ".",
                        wc[:1], wc[1:] + [_cite(elig, "base")], style)
        return _out("escalate-to-human", "undetermined", "The provisions give different time limits.",
                    f"The application is made {d} days after the declaration of results; the retrieved provisions give limits of {lo} and {hi} days.", wc, [], style, "conflict")

    if ptype == "re_registration":
        rr, _ = find(P, r_rereg)
        if not rr:
            return _out("escalate-to-human", "undetermined", "No re-registration provision was retrieved.", "The retrieved provisions do not cover re-registration.", [], [], style, "insufficient_evidence")
        g = f.get("grade_obtained")
        if g not in ("F", "DT"):
            ni, _ = find(P, r_no_improvement)
            return _out("reject-with-cited-clause", "not_eligible", f"Re-registration applies only to the grades F and DT; the grade obtained is {g}.",
                        f"{rr['clause_id']} provides re-registration for a student awarded the grade F or DT, and the grade obtained is {g}" + (f"; {ni['clause_id']} bars re-registration of a passed course for improving the grade." if ni else "."),
                        [_cite(ni or rr, "base")], [_cite(rr, "base")], style)
        mx = r_max_attempts(rr) or None
        ex, council = find(P, r_exhausted)
        used = f.get("attempts_used") or 0
        if mx and used >= mx:
            if ex:
                return _out("refer-to-committee", "undetermined", f"The {mx} attempts permitted are exhausted; continuation is reserved to the {council}.",
                            f"{rr['clause_id']} permits a maximum of {mx} attempts in total and the student has used {used}. {ex['clause_id']} bars further re-registration and refers continuation in the programme to the {council}.",
                            [_cite(ex, "modifier")], [_cite(rr, "base", r"maximum of")], style, "committee_reserved")
            return _out("reject-with-cited-clause", "not_eligible", f"The {mx} attempts permitted are exhausted.",
                        f"{rr['clause_id']} permits a maximum of {mx} attempts in total and the student has used {used}.", [_cite(rr, "base", r"maximum of")], [], style)
        return _out("grant-under-provision", "eligible", f"Grade {g} requires re-registration and attempts remain.",
                    f"The student was awarded the grade {g}; {rr['clause_id']} requires re-registration when the course is next offered and permits a maximum of {mx} attempts in total, of which {used} have been used.",
                    [_cite(rr, "base")], [], style)

    if ptype == "medical_leave":
        hod, n30 = find(P, r_leave_hod)
        com, rng = find(P, r_leave_committee)
        cap, ncap = find(P, r_leave_cap)
        L = f.get("leave_days")
        if not hod:
            return _out("escalate-to-human", "undetermined", "No medical leave provision was retrieved.", "The retrieved provisions do not cover medical leave.", [], [], style, "insufficient_evidence")
        if not f.get("medical_certificate_attached"):
            return _out("reject-with-cited-clause", "not_eligible", "Medical leave requires a certificate from a registered medical practitioner.",
                        f"{hod['clause_id']} allows medical leave on the basis of a certificate issued by a registered medical practitioner, and no medical certificate is attached.",
                        [_cite(hod, "base")], [], style)
        if L <= n30:
            return _out("grant-under-provision", "eligible", f"Leave of {L} days is within the {n30}-day limit the Head of Department may grant.",
                        f"{hod['clause_id']} allows the Head of Department to grant medical leave of up to {n30} consecutive days on the basis of a certificate issued by a registered medical practitioner; {L} days are requested and a certificate is attached.",
                        [_cite(hod, "base")], [], style)
        if cap and L > ncap:
            return _out("reject-with-cited-clause", "not_eligible", f"Medical leave exceeding {ncap} days in a semester shall not be granted.",
                        f"{L} days of leave are requested; {cap['clause_id']} states that medical leave exceeding {ncap} days in a semester shall not be granted.",
                        [_cite(cap, "base")], [_cite(hod, "base")], style)
        if com:
            cn = r_committee(com) or "committee"
            return _out("refer-to-committee", "conditionally_eligible_pending_committee", f"Leave of {L} days exceeds {n30} days and is reserved to the {cn}.",
                        f"{L} days of leave are requested, beyond the {n30} days the Head of Department may grant under {hod['clause_id']}; {com['clause_id']} reserves medical leave exceeding {rng[0]} days and up to {rng[1]} days to the {cn}.",
                        [_cite(com, "committee")], [_cite(hod, "base")], style, "committee_reserved")
        return _out("escalate-to-human", "undetermined", "No provision covering this length of leave was retrieved.", f"{L} days of leave exceed the {n30}-day limit in {hod['clause_id']}.", [_cite(hod, "base")], [], style, "insufficient_evidence")

    if ptype == "grade_grievance":
        sub, N = find(P, r_grievance_window)
        if not sub:
            return _out("escalate-to-human", "undetermined", "No grievance provision was retrieved.", "The retrieved provisions do not cover grade grievances.", [], [], style, "insufficient_evidence")
        d = f.get("days_since_result")
        fin, _ = find(P, r_grievance_final)
        if d <= N:
            cn = r_committee(sub) or "Grade Grievance Committee"
            m = re.search(r"to the ((?:[A-Z][a-z]+ )+Committee)", sub["text"])
            cn = m.group(1) if m else cn
            return _out("refer-to-committee", "conditionally_eligible_pending_committee", f"The grievance is within {N} days and is decided by the {cn}.",
                        f"The grievance is submitted {d} days after the declaration of results, within the {N} days allowed by {sub['clause_id']}." + (f" {fin['clause_id']} reserves the examination of each grievance to the {cn} and its decision shall be final." if fin else ""),
                        [_cite(fin or sub, "committee")], [_cite(sub, "base")], style, "committee_reserved")
        late, _ = find(P, r_grievance_late)
        return _out("reject-with-cited-clause", "not_eligible", f"The grievance is outside the {N}-day period.",
                    f"The grievance is submitted {d} days after the declaration of results, beyond the {N} days allowed by {sub['clause_id']}" + (f"; {late['clause_id']} states that a grievance received after that period shall not be entertained." if late else "."),
                    [_cite(late or sub, "base")], [_cite(sub, "base")], style)

    return _out("escalate-to-human", "undetermined", "The petition type is outside the regulations in the corpus.",
                "The petition does not fall within any petition type covered by the indexed regulations.", [], [], style, "out_of_corpus")

"""Published data contract (D1) for every external interface. JSON Schema is exported to contracts/ by
`python -m app.contracts`. Validation (schema, type, range, category, cross-field) happens at the API
boundary BEFORE any run is created; rejects are quarantined with field-level errors."""
from __future__ import annotations
import datetime as dt
import json
from enum import Enum
from typing import Literal, Optional
from pydantic import BaseModel, ConfigDict, Field, model_validator

CONTRACT_VERSION = "petition.v1"


class PetitionType(str, Enum):
    attendance = "attendance"
    re_evaluation = "re_evaluation"
    re_registration = "re_registration"
    medical_leave = "medical_leave"
    grade_grievance = "grade_grievance"


class Grounds(str, Enum):
    medical = "medical"
    sports_duty = "sports_duty"
    personal = "personal"
    academic = "academic"
    none = "none"


class PetitionIn(BaseModel):
    """A synthetic student petition. Real student records must never be submitted (student_ref must be SYN-######)."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    client_petition_ref: Optional[str] = Field(None, pattern=r"^[A-Za-z0-9_\-]{1,64}$", description="Idempotency key chosen by the caller")
    student_ref: str = Field(..., pattern=r"^SYN-\d{6}$", description="Synthetic student id only")
    program: Literal["BTECH", "MTECH", "MBA"]
    admission_batch: int = Field(..., ge=2015, le=2030)
    semester: int = Field(..., ge=1, le=8)
    course_code: str = Field(..., pattern=r"^[A-Z]{2,4}\d{3}$")
    course_type: Literal["theory", "lab", "project"] = "theory"
    declared_type: Optional[PetitionType] = None
    grounds: Grounds = Grounds.none
    description: str = Field(..., min_length=10, max_length=1500)
    submitted_on: dt.date
    # attendance
    classes_held: Optional[int] = Field(None, ge=1, le=300)
    classes_attended: Optional[int] = Field(None, ge=0, le=300)
    attendance_percent: Optional[float] = Field(None, ge=0, le=100)
    duty_classes: Optional[int] = Field(None, ge=0, le=300)
    prior_condonations: int = Field(0, ge=0, le=10)
    medical_certificate_attached: bool = False
    days_since_resumption: Optional[int] = Field(None, ge=0, le=365)
    # re-evaluation / grievance
    days_since_result: Optional[int] = Field(None, ge=0, le=365)
    prior_reevaluations: int = Field(0, ge=0, le=5)
    # re-registration
    grade_obtained: Optional[Literal["S", "A", "B", "C", "D", "E", "F", "DT"]] = None
    attempts_used: Optional[int] = Field(None, ge=0, le=10)
    # medical leave
    leave_days: Optional[int] = Field(None, ge=1, le=730)

    @model_validator(mode="after")
    def cross_field(self):
        errs = []
        if self.submitted_on > dt.date.today():
            errs.append(("submitted_on", "must not be in the future"))
        if self.classes_held is not None and self.classes_attended is not None:
            if self.classes_attended > self.classes_held:
                errs.append(("classes_attended", "must not exceed classes_held"))
            elif self.attendance_percent is not None:
                calc = 100.0 * self.classes_attended / self.classes_held
                if abs(calc - self.attendance_percent) > 0.5:
                    errs.append(("attendance_percent", f"inconsistent with classes_attended/classes_held ({calc:.1f})"))
        if (self.classes_held is None) != (self.classes_attended is None):
            errs.append(("classes_held", "classes_held and classes_attended must be supplied together"))
        if self.duty_classes is not None:
            if self.classes_held is None:
                errs.append(("duty_classes", "requires classes_held"))
            elif self.classes_attended is not None and self.duty_classes + self.classes_attended > self.classes_held:
                errs.append(("duty_classes", "classes_attended + duty_classes must not exceed classes_held"))
        req = {
            PetitionType.attendance: (self.attendance_percent is not None or self.classes_held is not None, "attendance_percent or classes_held/classes_attended"),
            PetitionType.re_evaluation: (self.days_since_result is not None, "days_since_result"),
            PetitionType.grade_grievance: (self.days_since_result is not None, "days_since_result"),
            PetitionType.re_registration: (self.grade_obtained is not None, "grade_obtained"),
            PetitionType.medical_leave: (self.leave_days is not None, "leave_days"),
        }
        if self.declared_type in req and not req[self.declared_type][0]:
            errs.append(("declared_type", f"{self.declared_type.value} petitions require {req[self.declared_type][1]}"))
        if errs:
            raise ValueError(json.dumps([{"field": f, "error": e} for f, e in errs]))
        return self

    def effective_attendance(self) -> Optional[float]:
        if self.classes_held and self.classes_attended is not None:
            return round(100.0 * self.classes_attended / self.classes_held, 1)
        return self.attendance_percent


Route = Literal["grant-under-provision", "refer-to-committee", "reject-with-cited-clause", "escalate-to-human"]


class Citation(BaseModel):
    clause_id: str
    doc_title: str
    chapter: int
    chapter_title: str
    heading: str
    quote: str
    role: str = "supporting"


class Escalation(BaseModel):
    required: bool
    reason_code: Optional[str] = None   # committee_reserved | conflict | out_of_corpus | unverifiable | guardrail | degraded | failed
    reason: Optional[str] = None
    path: list[str] = []


class RunResult(BaseModel):
    run_id: str
    route: Route
    eligibility_position: Literal["eligible", "not_eligible", "conditionally_eligible_pending_committee", "undetermined"]
    petition_type: Optional[str]
    basis: str
    rationale: str
    citations: list[Citation]
    escalation: Escalation
    verification: dict
    degraded: bool = False
    decision_authority: Literal["examination_officer"] = "examination_officer"
    disclaimer: str = "Recommendation only. The examination officer decides this petition."
    prompt_versions: dict = {}
    corpus_version: Optional[str] = None
    guardrail_flags: list[str] = []
    cost: dict = {}


class RunAccepted(BaseModel):
    run_id: str
    status: str
    status_url: str
    cached: bool = False


class Feedback(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str = Field(..., pattern=r"^run_[a-f0-9]{16}$")
    satisfactory: bool
    reason: str = Field(..., min_length=3, max_length=500)


def flatten_errors(exc) -> list[dict]:
    """pydantic ValidationError -> field-level list."""
    out = []
    for e in exc.errors():
        if e["type"] == "value_error" and str(e.get("msg", "")).find("[{") >= 0:
            try:
                out += json.loads(e["msg"][e["msg"].index("["):])
                continue
            except Exception:
                pass
        out.append({"field": ".".join(str(x) for x in e["loc"]) or "body", "error": e["msg"]})
    return out


if __name__ == "__main__":
    import os
    os.makedirs("contracts", exist_ok=True)
    for name, m in [("petition.request", PetitionIn), ("run.result", RunResult), ("run.accepted", RunAccepted), ("feedback.request", Feedback)]:
        json.dump(m.model_json_schema(), open(f"contracts/{name}.schema.json", "w"), indent=2)
    print("schemas written to contracts/")

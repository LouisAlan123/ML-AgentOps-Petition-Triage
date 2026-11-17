"""Generates eval/scenarios.jsonl - the fixed evaluation set (L3). Each scenario: petition, expected route,
expected escalation reason (if any) and the passages that support the expected outcome (gold clause ids).
Run once; the jsonl is committed so the set is fixed."""
import json

B = dict(student_ref="SYN-000100", program="BTECH", admission_batch=2024, semester=5, course_code="CS301", submitted_on="2026-11-01")
S = []


def add(sid, title, petition, route, support, esc=None, tags=()):
    S.append({"id": sid, "title": title, "petition": {**B, **petition}, "expected_route": route,
              "expected_escalation": esc, "supporting_clauses": support, "tags": list(tags)})


A = dict(declared_type="attendance", classes_held=60)
add("ATT-01", "Section 4.1 hard case: 68.3% attendance, hospitalised, certificate in time",
    {**A, "classes_attended": 41, "grounds": "medical", "medical_certificate_attached": True, "days_since_resumption": 3,
     "description": "I was hospitalised with dengue for two weeks and could not attend classes. My attendance is short. Certificate attached."},
    "refer-to-committee", ["REG-3.1", "REG-7.4"], "committee_reserved", ["hard_case_4_1"])
add("ATT-02", "4.1 variant: 70% with surgery, no mention of shortfall words",
    {**A, "classes_attended": 42, "grounds": "medical", "medical_certificate_attached": True, "days_since_resumption": 5,
     "description": "I underwent an appendix surgery in September and missed lectures. Requesting permission to write the ESE."},
    "refer-to-committee", ["REG-3.1", "REG-7.4"], "committee_reserved", ["hard_case_4_1"])
add("ATT-03", "4.1 variant: exactly 65%, timing of certificate not stated",
    {**A, "classes_attended": 39, "grounds": "medical", "medical_certificate_attached": True,
     "description": "Medical issue (typhoid) kept me away from classes for most of August. Doctor's certificate enclosed."},
    "refer-to-committee", ["REG-3.1", "REG-7.4"], "committee_reserved", ["hard_case_4_1", "boundary"])
add("ATT-04", "Attendance 80% - no shortfall",
    {**A, "classes_attended": 48, "grounds": "none", "description": "Please confirm I am eligible to write the ESE for CS301 this semester."},
    "reject-with-cited-clause", ["REG-3.1"])
add("ATT-05", "Attendance 55% medical - below condonable floor",
    {**A, "classes_attended": 33, "grounds": "medical", "medical_certificate_attached": True, "days_since_resumption": 2,
     "description": "I was ill with jaundice for over a month and my attendance dropped badly. Please condone."},
    "reject-with-cited-clause", ["REG-7.5", "REG-7.4"])
add("ATT-06", "Attendance 70%, personal grounds",
    {**A, "classes_attended": 42, "grounds": "personal", "description": "I had to travel home for a family function and missed classes. Kindly condone the shortage."},
    "reject-with-cited-clause", ["REG-7.4"])
add("ATT-07", "Attendance 70%, medical but no certificate",
    {**A, "classes_attended": 42, "grounds": "medical", "medical_certificate_attached": False,
     "description": "I had a bad fever for two weeks so my attendance is low. I did not visit a doctor."},
    "reject-with-cited-clause", ["REG-7.4"])
add("ATT-08", "Attendance 70%, condonation already used twice",
    {**A, "classes_attended": 42, "grounds": "medical", "medical_certificate_attached": True, "days_since_resumption": 1, "prior_condonations": 2,
     "description": "I was hospitalised again this semester. Certificate attached. Please condone my attendance shortage."},
    "reject-with-cited-clause", ["REG-7.6"])
add("ATT-09", "Sports duty brings attendance to 75%",
    {**A, "classes_attended": 41, "duty_classes": 6, "grounds": "sports_duty",
     "description": "I represented the University at the inter-university athletics meet with the Dean's approval and missed classes."},
    "grant-under-provision", ["REG-3.5", "REG-3.1"])
add("ATT-10", "Conflict: certificate submitted 9 days after resuming (REG 7 days vs BYE 10 days)",
    {**A, "classes_attended": 42, "grounds": "medical", "medical_certificate_attached": True, "days_since_resumption": 9,
     "description": "I was admitted to hospital for pneumonia. I submitted my medical certificate after resuming classes."},
    "escalate-to-human", ["REG-7.4", "BYE-8.2"], "conflict", ["conflict"])
add("ATT-11", "Certificate submitted 15 days after resuming - late under both",
    {**A, "classes_attended": 42, "grounds": "medical", "medical_certificate_attached": True, "days_since_resumption": 15,
     "description": "I was hospitalised for a fracture. I could only submit the medical certificate recently."},
    "reject-with-cited-clause", ["REG-7.4"])
R = dict(declared_type="re_evaluation")
add("REV-01", "Re-evaluation within 5 days, theory", {**R, "days_since_result": 3, "description": "I expected a much higher score in CS301. Request re-evaluation of my answer script."},
    "grant-under-provision", ["REG-9.1", "REG-9.2"])
add("REV-02", "Conflict: re-evaluation on day 6 (REG 5 days vs BYE 7 days)", {**R, "days_since_result": 6, "description": "Please re-evaluate my CS301 answer script. I was travelling when results came out."},
    "escalate-to-human", ["REG-9.2", "BYE-14.3"], "conflict", ["conflict"])
add("REV-03", "Re-evaluation on day 12 - late under both", {**R, "days_since_result": 12, "description": "I would like a re-evaluation of my ESE paper for CS301."},
    "reject-with-cited-clause", ["REG-9.2"])
add("REV-04", "Re-evaluation of a laboratory course", {**R, "days_since_result": 2, "course_type": "lab", "course_code": "CS391", "description": "Request re-evaluation of my lab ESE marks for CS391."},
    "reject-with-cited-clause", ["REG-9.3"])
add("REV-05", "Second re-evaluation of same course", {**R, "days_since_result": 2, "prior_reevaluations": 1, "description": "I want my answer script re-evaluated again, the first re-evaluation was unfair."},
    "reject-with-cited-clause", ["REG-9.5"])
G = dict(declared_type="re_registration")
add("REG-01", "Re-registration after grade F, 1 attempt used", {**G, "grade_obtained": "F", "attempts_used": 1, "description": "I failed CS301 and wish to re-register for the course in the next semester."},
    "grant-under-provision", ["REG-5.3"])
add("REG-02", "Attempts exhausted", {**G, "grade_obtained": "F", "attempts_used": 3, "description": "I have failed CS301 three times. Requesting one more chance to re-register."},
    "refer-to-committee", ["REG-5.3", "REG-5.4"], "committee_reserved")
add("REG-03", "Re-registration to improve a passed grade", {**G, "grade_obtained": "C", "attempts_used": 1, "description": "I passed CS301 with grade C and want to re-register to improve my grade."},
    "reject-with-cited-clause", ["REG-5.6"])
M = dict(declared_type="medical_leave", grounds="medical")
add("MED-01", "Medical leave 20 days with certificate", {**M, "leave_days": 20, "medical_certificate_attached": True, "description": "Requesting medical leave of 20 days for a knee surgery and recovery."},
    "grant-under-provision", ["REG-6.2"])
add("MED-02", "Medical leave 60 days", {**M, "leave_days": 60, "medical_certificate_attached": True, "description": "I need medical leave for two months due to a serious illness requiring treatment."},
    "refer-to-committee", ["REG-6.3"], "committee_reserved")
add("MED-03", "Medical leave 200 days", {**M, "leave_days": 200, "medical_certificate_attached": True, "description": "I need leave for most of the semester and more for a long treatment."},
    "reject-with-cited-clause", ["REG-6.4"])
add("MED-04", "Medical leave without certificate", {**M, "leave_days": 10, "medical_certificate_attached": False, "description": "I was unwell and need leave of absence for ten days."},
    "reject-with-cited-clause", ["REG-6.2"])
V = dict(declared_type="grade_grievance")
add("GRV-01", "Grade grievance within 10 days", {**V, "days_since_result": 4, "description": "I believe my grade in CS301 was wrongly awarded; the grading did not match the scheme."},
    "refer-to-committee", ["REG-10.1", "REG-10.2"], "committee_reserved")
add("GRV-02", "Grade grievance after 20 days", {**V, "days_since_result": 20, "description": "I want to raise a grievance about my CS301 grade awarded last month."},
    "reject-with-cited-clause", ["REG-10.1", "REG-10.3"])
add("OUT-01", "Out of corpus: branch change", {"description": "I want to change my branch from CSE to ECE from the next semester."},
    "escalate-to-human", [], "out_of_corpus", ["out_of_corpus"])
add("OUT-02", "Out of corpus: hostel fee", {"description": "Request waiver of hostel fee for this semester because of financial difficulties."},
    "escalate-to-human", [], "out_of_corpus", ["out_of_corpus"])
add("OUT-03", "Out of corpus: MBA programme (corpus is BTECH 2024)", {"program": "MBA", "declared_type": "attendance", "classes_held": 60, "classes_attended": 42, "grounds": "medical",
    "medical_certificate_attached": True, "description": "My attendance is short due to hospitalisation. Please condone."},
    "escalate-to-human", [], "out_of_corpus", ["out_of_corpus"])
add("OUT-04", "Out of corpus: 2021 batch", {"admission_batch": 2021, "declared_type": "re_evaluation", "days_since_result": 2,
    "description": "Request re-evaluation of my answer script for CS301."}, "escalate-to-human", [], "out_of_corpus", ["out_of_corpus"])

with open("eval/scenarios.jsonl", "w") as f:
    for s in S:
        f.write(json.dumps(s) + "\n")
print(len(S), "scenarios")

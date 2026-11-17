"""D6 load test. Each simulated user submits a petition, polls until the run finishes, and reports the full
submission-to-final-result time as the custom request "run_e2e". Run the system with GATEWAY_PROVIDER=heuristic
(the stub: fixed reasoning with realistic latency) so the test measures our system, not a provider's rate limits.

  locust -f loadtest/locustfile.py --headless -u 20 -r 5 -t 60s --host http://localhost:8000 --csv out/u20
CACHE_MODE=miss (default) makes every petition unique; CACHE_MODE=mixed repeats a small set of petitions (cache hits)."""
import datetime as dt, os, random, time, uuid
from locust import HttpUser, task, between, events

MODE = os.environ.get("CACHE_MODE", "miss")
TODAY = str(dt.date.today())
TEMPLATES = [
    dict(declared_type="attendance", grounds="medical", classes_held=60, classes_attended=41, medical_certificate_attached=True, days_since_resumption=3,
         description="I was hospitalised with dengue and missed classes. Certificate attached."),
    dict(declared_type="re_evaluation", days_since_result=2, description="Please re-evaluate my answer script; I expected a higher score."),
    dict(declared_type="re_registration", grade_obtained="F", attempts_used=1, description="I failed the course and want to re-register next semester."),
    dict(declared_type="medical_leave", grounds="medical", leave_days=20, medical_certificate_attached=True, description="Requesting medical leave for surgery recovery."),
    dict(declared_type="grade_grievance", days_since_result=3, description="My grade was wrongly awarded; requesting a review by the committee."),
]


class Officer(HttpUser):
    wait_time = between(1, 3)

    def on_start(self):
        self.cid = "lt-" + uuid.uuid4().hex[:8]

    @task
    def submit_and_wait(self):
        t = random.choice(TEMPLATES)
        p = dict(student_ref="SYN-%06d" % random.randint(0, 999999), program="BTECH", admission_batch=2024, submitted_on=TODAY,
                 course_code=random.choice(["CS301", "CS302", "MA201"]), semester=random.randint(1, 8) if MODE == "miss" else 5, **t)
        if MODE == "mixed":
            p["course_code"] = "CS301"
        else:   # unique facts => whole-run cache miss, so every run executes the full agent loop
            p["description"] = p["description"] + f" (ref {uuid.uuid4().hex[:10]})"
        t0 = time.time()
        with self.client.post("/v1/petitions", json=p, headers={"x-client-id": self.cid}, name="submit", catch_response=True) as r:
            if r.status_code != 202:
                r.failure(f"submit {r.status_code}"); return
            url = r.json()["status_url"]
        status = "?"
        while time.time() - t0 < 120:
            s = self.client.get(url, name="poll").json()
            status = s["status"]
            if status in ("completed", "failed"):
                break
            time.sleep(0.25)
        ms = (time.time() - t0) * 1000
        exc = None if status == "completed" else Exception(f"run {status}")
        events.request.fire(request_type="RUN", name="run_e2e", response_time=ms, response_length=0, exception=exc, context={})

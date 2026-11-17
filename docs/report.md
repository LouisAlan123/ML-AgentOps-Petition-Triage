# Mini Project 15: Academic Regulation and Student Petition Triage Service
UE24AM342AA5 ML System Design & AgentOps · Team: _<names, SRNs>_

## 1. Problem and scope
An examination section decides student petitions (attendance condonation, re-evaluation, re-registration, medical
leave, grade grievance) against regulations where the base rule and the provision that modifies it sit in different
chapters, often without a cross-reference. We built a multi-agent service that validates a petition, determines the
position under the regulations, and routes it with a cited justification. **It never decides.** The examination
officer does. The system says *grant-under-provision*, *refer-to-committee*, *reject-with-cited-clause*, or
escalates to a human when provisions conflict, discretion is reserved to a committee, or the petition is outside the
corpus. Synthetic petitions only (`student_ref` must match `SYN-######`).

**Corpus.** A synthetic "Academic Regulations 2024" (12 chapters, 45 clauses) and "Examination Bye-Laws 2024"
(13 clauses), published as text-layer PDFs. They reproduce the difficulty in section 3: the 75 % attendance rule is in
Chapter 3; condonation for 65-75 % on medical grounds is in Chapter 7, reserved to a committee, with no forward
reference. The Bye-Laws give different time limits from the Regulations for re-evaluation (5 vs 7 days) and medical
certificates (7 vs 10 days). _[If you swap in your university's PDFs, update this paragraph and `eval/scenarios.jsonl`.]_

**Section 4.1 reference answer.** 41/60 classes = 68.3 %, medical grounds, certificate in 3 days →
**refer-to-committee**, citing REG-3.1 (base: 75 % minimum), REG-3.4 (consequence), REG-7.4 (condonation 65-75 %,
certified illness, decided by the Condonation Committee) and REG-7.6 (limit: twice per programme).

## 2. Agent design
Two LangGraph agents in separate, independently scaled services (docs/architecture.md).
* **Researcher**: classify → scope check → bounded plan/act loop over 5 tools: `search_corpus`, `get_clause`,
  `get_petition_fields`, `find_citing_clauses`, `list_clauses_by_topic`. It retrieves the base provision, looks for
  modifiers via reverse references and a query derived from the base clause text, checks the Bye-Laws, and fetches the
  precedence clause when limits differ. Limits: 10 steps, 10 tool calls, 25 s per round; a run deadline of 120 s.
* **Adjudicator**: precheck (out-of-corpus / conflict / degraded) → draft → output guard → **verify** → finalise.
  On verification failure it sends the run back to the researcher with `strategy=alt` (enumerate the topic + keyword
  search) once, then escalates. An unverified answer is never returned.
* **Hand-off:** dossier in Redis + a message on a Redis stream. A crashed worker's message is re-claimed by another replica.

## 3. Architecture
See `docs/architecture.png`. Services: api, researcher ×N, adjudicator, llm-gateway, indexer (job), redis,
Prometheus, Grafana, Jaeger, cAdvisor. Boundaries are justified by scaling (researcher), failure isolation,
independent release (gateway, verifier) and resource profile (index in RAM; indexer as a batch burst).

## 4. Design decisions, and what we rejected
* Retrieval is a **tool**, not a stage; the reverse-reference tool fixes the missing-cross-reference problem structurally.
* **Deterministic verifier**, not an LLM judge: reproducible, free, and it caught our own bug (incident 5).
* **Model behind a gateway we control:** provider = config; faults injectable for the demo; offline provider for tests and load tests.
* **Polling + Redis Streams** over callbacks or Celery (see architecture.md).
* **Rejected:** one process for both agents; a cross-encoder re-ranker; caching degraded or unverified results.

## 5. Evaluation (29 scenarios, docs/evaluation.md)
Metric definitions are in docs/evaluation.md (deterministic, TruLens-equivalent).

**Deliberate change: single-pass retrieval → agentic retrieval**

| | groundedness | faithfulness | context relevance | hallucination | route acc. | citation recall |
|---|---|---|---|---|---|---|
| before (single_pass) | 1.000 | 1.000 | 0.670 | 0.000 | 0.586 | 0.500 |
| after (agentic) | 1.000 | 1.000 | 0.476 | 0.000 | **1.000** | **1.000** |

The four quality metrics barely move because the verifier makes single-pass *abstain* rather than hallucinate. The gain
shows in accuracy and recall, including the 4.1 case and the conflict case, which single-pass got confidently wrong.
**Prompt variants (L2):** `draft_position` v2 beats v1 on citation recall by +0.62 and route accuracy by +0.52.

**Limitation:** these numbers use the deterministic offline provider; re-run with a real model via `GATEWAY_PROVIDER=openai_compat`.

## 6. Performance and cost
* **SLO** (declared first, docs/slo.md): p95 end-to-end ≤ 10 s at 20 concurrent users, stub at 400 ms/call.
* **Measured:** 1 researcher meets it at 10 users (p95 8.7 s) and fails at 20 (22 s). 3 researchers meet it at 20
  (p95 6.5 s, 2.95 runs/s) and fail at 40 (14 s).
* **Bottleneck:** researcher concurrency blocked on sequential model calls (research queue ~30, adjudicate queue ~0,
  CPU < 25 %). Scaling researchers helps; scaling the api would not.
* **Cache:** p95 5.9 → 3.9 s and cost/run −94 % on a repeat-heavy workload; no effect on unique petitions.
* **Cost:** $0.21 per 100 runs at reference rates (~9 model calls/run). Largest driver: `researcher.plan`, 81 %
  (the prompt grows with every step).

## 7. What did not work / limitations
* The offline provider is a rule-based stand-in for the LLM. It reads thresholds from clause text but is tuned to this
  corpus. Real-model numbers must be re-measured.
* Hashing embeddings are weak semantically (a pure semantic search misses the base rule). Hybrid search plus the agent
  compensates; sentence-transformers is supported but untested in our environment (model download blocked).
* Context relevance falls with the agent, which reads beyond the topic on purpose. The metric penalises the right behaviour.
* The planner resends all observations each step: the main cost driver, and fixable by summarising.
* Load tests were run on native processes in a 2-vCPU sandbox. _[Replace with your Docker Compose numbers.]_
* Toxicity detection is a lexicon, not a model.

# Evaluation (L2, L3)

`eval/scenarios.jsonl` holds **29 fixed scenarios** (generator: `eval/build_scenarios.py`). Each has the petition, the
expected route, the expected escalation reason and the gold supporting clauses. There are 3 section-4.1 variants,
2 conflict cases, 4 out-of-corpus cases, and the remainder cover all five petition types.

```
python -m app.eval.run_eval                                        # current config
python -m app.eval.run_eval --compare-prompts v1 v2                # L2
python -m app.eval.run_eval --compare-strategy single_pass agentic # L3 before/after
```

## How each metric is computed (`app/eval/metrics.py`, deterministic, no LLM judge, "TruLens-equivalent")
*Claims* = sentences of the rationale. A claim is *supported* by a set of texts if ≥ 60 % of its content tokens
(stemmed; stop-words, routing words and clause ids removed) occur in those texts or in the petition's own fields,
**and** every number in it occurs there (or is arithmetic on petition fields, e.g. attendance %).

| Metric | Definition |
|---|---|
| Groundedness | mean over claims of *supported by all passages retrieved in the run* |
| Faithfulness | mean over claims of *supported by only the passages the answer cites* |
| Context relevance | fraction of retrieved passages whose topic tags match the petition type |
| Hallucination rate | fraction of runs with any unsupported claim, unresolved citation or non-verbatim quote |
| (extra) citation recall | gold clauses cited / gold clauses |
| (extra) context recall | gold clauses retrieved / gold clauses |

Abstentions (out-of-corpus, degraded, insufficient-evidence escalations) are excluded from groundedness/faithfulness
and count as non-hallucinating.

## L3: one deliberate change, before and after
**Change:** replace the straightforward single-pass retrieval (embed the petition, take the top 4 passages, answer) with
the agentic loop (base provision → find modifiers → derived query → verify). Toggle: `SEARCH_STRATEGY`.

| metric | before: single_pass | after: agentic |
|---|---|---|
| **groundedness** | 1.000 | 1.000 |
| **faithfulness** | 1.000 | 1.000 |
| **context relevance** | 0.670 | 0.476 |
| **hallucination rate** | 0.000 | 0.000 |
| route accuracy | 0.586 | **1.000** |
| escalation accuracy | 0.862 | **1.000** |
| section 4.1 hard case | fail | **pass** |
| citation recall | 0.500 | **1.000** |
| context recall | 0.680 | **1.000** |
| LLM calls / run | 1.9 | 8.9 |
| cost / 100 runs | $0.024 | $0.193 |

Reading it honestly:
* The four required metrics barely move, and **that is the finding**. Single-pass does not hallucinate here because
  the verifier refuses unsupported answers, so it *abstains* instead ("no attendance rule was retrieved"). Its failure
  is missing evidence, which shows up in route accuracy and citation/context recall.
* Worse than abstaining, single-pass gave **confident wrong answers** on REV-02 (rejected a re-evaluation that the
  Bye-Laws would allow, because it never saw the conflicting Bye-Law) and missed REG-10.2 on the grievance cases.
* Context relevance *drops* with the agent because it deliberately reads beyond the petition's topic (Bye-Laws,
  precedence clause). The metric penalises that; the outcome metrics show it is the right call.
* Cost goes up about 8×. That is the price of the agent, and why A1's loop limits and D7's cache exist.

## L2: two prompt variants of `draft_position` on the same set
* **v1** "Cite the primary provision that determines the route."
* **v2** "Apply every provision that binds: the base rule AND any modifier; cite both; quotes verbatim; numbers only from fields or cited text."

| metric | v1 | v2 |
|---|---|---|
| route accuracy | 0.483 | **1.000** (+0.517) |
| citation recall | 0.380 | **1.000** (+0.620) |
| verification retry rate | 0.517 | **0.000** |
| hallucination rate | 0 | 0 |
| cost / 100 runs | $0.320 | **$0.193** |

**v2 is better, most of all on citation recall (+62 points) and route accuracy (+52 points).** v1 answers cite one
clause while the rationale relies on others, so the verifier rejects them (`mentioned_clauses_cited`,
`modifier_and_base_both_cited`). The researcher retries, then the run escalates. v1 is therefore also more expensive.

**Caveat to state in the viva:** these numbers come from the deterministic offline provider, which implements
v1 vs v2 as the behaviour each instruction asks for. They show the harness, the metrics and the verifier working.
To report a real model's numbers, set `GATEWAY_PROVIDER=openai_compat` and re-run the same commands. The harness does
not change.

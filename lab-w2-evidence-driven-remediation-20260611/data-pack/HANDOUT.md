# Lab — Evidence-Driven Remediation Engine

**Individual.**

---

## 1. The brief

You are joining a platform team that operates ~10 microservices in production. The on-call rotation receives roughly 5 incidents per week. About 80% of those incidents repeat a pattern that has been seen before, but every incident still costs the on-call engineer 15-20 minutes of dashboard reading before they decide what to do.

The team wants an **evidence-driven remediation engine**. Input: a structured incident report with logs, traces, metrics, and topology. Output: a recommended action with a calibrated confidence score and a transparent justification chain that on-call can audit in 30 seconds.

> Three things the team will reject:
> - Any rule of the form `if root_cause.class == X then action = Y`. The engine must derive the answer, not look it up.
> - Any decision the engine cannot explain. If the recommendation lands without traceable evidence, on-call will not trust it.
> - Any system that silently fails on novel incidents. If the input does not look like anything seen before, the engine must say so and escalate.

You have the historical incident corpus and a service topology. Build the engine.

---

## 2. Inputs you receive

### 2.1 The evidence package — one JSON per incident

For each incident the engine receives a self-contained JSON file. Schema:

```json
{
  "incident_id": "INC-2026-06-10-001",
  "detected_at": "2026-06-10T14:23:00Z",
  "trigger_alert": {
    "service": "checkout-svc",
    "rule_id": "latency-p99-high",
    "severity": "critical"
  },
  "topology": {
    "nodes": [{"id": "edge-lb", "tier": "edge"}, ...],
    "edges": [{"from": "checkout-svc", "to": "payment-svc", "protocol": "http"}, ...]
  },
  "metrics_window": {
    "from": "2026-06-10T13:53:00Z",
    "to":   "2026-06-10T14:30:00Z",
    "samples": {
      "payment-svc.cpu":             [[ts, value], ...],
      "payment-svc.latency_p99_ms":  [[ts, value], ...],
      "checkout-svc.latency_p99_ms": [[ts, value], ...]
    }
  },
  "traces": [
    {"ts": "2026-06-10T14:23:05Z", "from": "checkout-svc", "to": "payment-svc",
     "count": 71, "error_count": 44, "p50_ms": 280, "p99_ms": 2410},
    ...
  ],
  "logs": [
    {"ts": "2026-06-10T14:22:51Z", "svc": "payment-svc", "level": "ERROR",
     "msg": "ConnectionPool: timeout acquiring connection (waited 5000ms)"},
    ...
  ]
}
```

Approximate sizes per incident: ~30 metric series of ~100 samples each, ~80 trace records, ~500 log lines. Window spans 30-45 minutes around onset.

### 2.2 Historical incident corpus — `incidents_history.json`

A list of ~30 past incidents. Each entry:

```json
{
  "id": "INC-2025-11-08",
  "root_cause_class": "connection_pool_exhaustion",
  "affected_services": ["payment-svc", "checkout-svc"],
  "log_signatures": [
    "ConnectionPool: timeout acquiring connection",
    "Failed to forward request: pool exhausted"
  ],
  "trace_signatures": [
    {"from": "checkout-svc", "to": "payment-svc", "p99_deviation_ratio": 2.8, "error_rate": 0.31}
  ],
  "metric_signatures": [
    {"service": "payment-svc", "metric": "conn_pool_used", "delta": "30 -> 99"}
  ],
  "actions_taken": ["rollback_service:payment-svc:v3.1", "increase_pool_size:payment-svc:50->100"],
  "outcome": "success",
  "mttr_minutes": 12
}
```

Distribution of `outcome` values across the 30: roughly 65% success, 25% partial (mitigated but not root-cause fixed), 10% failed.

### 2.3 Action catalog — `actions.yaml`

A static catalog of remediation actions the engine may recommend. Each action has cost and blast-radius metadata.

```yaml
- name: rollback_service
  params: [service, target_version]
  cost_min: 10
  downtime_min: 2
  blast_radius_services: 1
  rollback_window_sec: 60
- name: increase_pool_size
  params: [service, from_value, to_value]
  cost_min: 1
  downtime_min: 0
  blast_radius_services: 1
  rollback_window_sec: 30
- name: restart_pod
  params: [service, pod_selector]
  cost_min: 2
  downtime_min: 1
  blast_radius_services: 1
  rollback_window_sec: 90
- name: dns_config_rollback
  params: [configmap_name, target_revision]
  cost_min: 5
  downtime_min: 3
  blast_radius_services: 3
  rollback_window_sec: 120
- name: network_policy_revert
  params: [policy_name]
  cost_min: 15
  downtime_min: 5
  blast_radius_services: 4
  rollback_window_sec: 180
- name: page_oncall
  params: [team]
  cost_min: 0
  downtime_min: 0
  blast_radius_services: 0
  rollback_window_sec: 0
```

### 2.4 Eval set — `eval/E01.json` ... `eval/E08.json`

Eight evaluation incident JSONs of varying difficulty:

| File | Class | Difficulty |
|---|---|---|
| `E01-E04` | Known patterns matching ≥1 historical incident closely | easy — single dominant signal |
| `E05` | Two historical incidents tie on similarity; voting must break the tie | medium — outcome weighting matters |
| `E06` | Conflicting evidence — logs point at one service, traces at another | hard — pick the side and justify |
| `E07` | Novel pattern (no close historical match) — engine should escalate, not guess | hard — OOD handling |
| `E08` | Cascade across 4 services with the root being the *leaf*, not the alerting service | hard — topology + trace reasoning |

`eval/expected.json` provides the accepted action(s) per incident:

```json
{
  "E01": {
    "accepted_actions": [
      {"name": "rollback_service",   "params": {"service": "payment-svc"}},
      {"name": "increase_pool_size", "params": {"service": "payment-svc"}}
    ],
    "must_not_action": "page_oncall",
    "notes": "Two actions are acceptable. Engine must NOT escalate on this one."
  },
  "E07": {
    "accepted_actions": [{"name": "page_oncall"}],
    "notes": "OOD — engine must escalate. Any auto-action is wrong."
  }
}
```

Grading uses `accepted_actions` (multiple may be acceptable) and `must_not_action` (an action explicitly forbidden — escalating when not warranted is also wrong, not only failing to escalate).

### 2.5 What's in the data pack you download

A single zip containing:

```
data-pack/
├── eval/
│   ├── E01.json ... E08.json
│   └── expected.json
├── incidents_history.json     (~30 entries)
├── actions.yaml               (the catalog from §2.3)
├── topology.json              (the canonical topology — same as embedded per-incident)
└── README.md                  (download instructions, schema notes)
```

Everything is plain JSON / YAML, no binary, no dependency on external services.

### 2.6 Schema details easy to overlook

These bite people who skim:

- **`actions_taken` in the historical corpus is a list of strings in the form `"action_name:param1:param2:..."`.** You must parse this into the `actions.yaml` schema yourself. Example: `"rollback_service:payment-svc:v3.1"` → `{"name": "rollback_service", "params": {"service": "payment-svc", "target_version": "v3.1"}}`.
- **`metric_signatures[*].delta`** in the historical corpus is a string like `"30 -> 99"`. Parse the two numbers and use the ratio or absolute change — pick one and be consistent.
- **`affected_services` is in historical entries but not in live incident JSON.** You must derive it from the live evidence (any service that appears in `trigger_alert`, has anomalous traces, or has burst log activity). Document your rule in `FINDINGS.md`.
- **The live `logs[*].msg` field is the raw line.** Historical entries have `log_signatures` as cleaned templates. You must convert raw → template-cluster before you can compare. Drain or any clustering technique works.
- **`target_version` for `rollback_service`** is not provided in the live incident. Use `"previous"` as a placeholder string — the action recommendation does not need to know which exact version, only that a rollback is wanted.

---

## 3. What you must build — REQUIRED

### Getting started — your first 30 minutes (read this before opening an editor)

1. **Read one incident, end to end.** Open `eval/E01.json`. Scroll through the logs (raw lines with timestamps). Look at one trace record. Read the `trigger_alert`. Get a feel for the *shape* of an incident before deciding how to represent it.
2. **Read three historical entries** from `incidents_history.json`. Notice the schema differs from the live incident: historical has *aggregated signatures* (`log_signatures` = cleaned templates, `trace_signatures` = per-edge deviations, `metric_signatures` = delta strings); live has *raw* logs and traces. The first job your code has is bridging these two representations.
3. **Sketch the pipeline on paper.** Three blocks: feature extraction → retrieval → action selection. Draw what data shape moves between them. Decide before you code.
4. **Pick one incident as your smoke test.** Get end-to-end working on E01 first — even with placeholder logic in some layers. End-to-end stub > each-layer-perfect.

**Common pitfalls to avoid:**
- Treating raw log lines as features directly. The historical corpus has *templates*. You must bridge.
- Using only metrics. The three red flags in §1 specifically reject metric-only reasoning.
- Implementing all three layers in isolation. Easier to debug if you wire stubs first, then refine.
- Spending an hour on schema parsing. The data pack ships `optional-helpers.py` with two pure-mechanical parsers for the schema gotchas — feel free to import or to write your own.

---

The system is conceptually three layers: **make sense of the incident, compare it to past incidents, choose an action**. How you implement each layer is your design call — the lab is graded on outcomes and reasoning, not on whether you used a specific algorithm. Reference reading in §9 lists options.

### Layer 1 — Make the incident comparable

You receive raw evidence (logs, traces, metrics, topology). To compare a new incident against your historical corpus you need a representation that exposes what matters and ignores what doesn't.

**Must:** your representation draws signal from **both** logs and traces (not metrics alone — metrics drift slowly and are weak signal for class-of-incident).

**Choices that are yours to make:** text vs. numeric vs. hybrid; what gets normalised, what stays raw; how you collapse 500 log lines into something stable; what counts as the "baseline" portion of the window; whether topology participates as a feature or as a constraint elsewhere.

**Things worth thinking about:** the historical corpus has ~29 entries. A 1024-dim embedding may overfit on so few neighbours. A 5-dim hand-engineered feature may underfit. Where do you land, and why?

### Layer 2 — Find precedents, derive a candidate action

The historical corpus has the wisdom: what was tried before, on what kind of incident, and whether it worked. Your job in this layer is to (a) decide which past incidents are similar enough to be relevant, (b) extract a ranked list of candidate actions from them.

**Must:**
- Your ranking distinguishes actions that historically *succeeded* from actions that historically *failed* on similar incidents. A failed action should not be ranked first just because it appeared in the closest neighbour.
- Your mechanism degrades gracefully when nothing in the corpus is close to the input. The engine should not silently pick a top-1 neighbour at distance 0.99 and act on it.

**Choices that are yours to make:** similarity / distance function; how many neighbours to consider (or whether to use a threshold instead of top-k); how outcomes weight votes; whether to model uncertainty explicitly or implicitly.

**Things worth thinking about:** retrieval-augmented decisioning is the same shape as RAG in NLP. The literature on that field has working patterns (re-ranking, weighted fusion, hybrid retrieval). Not all transfer; some do.

### Layer 3 — Choose the action

A candidate list isn't a decision. The action you ship has a cost (someone pays it), a blast radius (some services break if it goes wrong), and a confidence (your own estimate of whether you're right). Selection must reflect all three.

**Must:**
- The engine should not silently default to `page_oncall` whenever uncertain. Page is the *last* resort, not the first. (Note that `page_oncall` has zero cost in `actions.yaml` — naive utility math will make it always-optimal. Account for this.)
- The engine should not silently auto-act on the highest-confidence candidate when blast radius is large. Some actions carry consequences a 35%-confident engine has no business triggering.

**Choices that are yours to make:** how to combine confidence with cost; what threshold pattern separates auto-action from escalation; how to handle ties or near-ties between candidates; how to deal with the "no good candidate at all" case.

**Things worth thinking about:** read §9 on cost-aware decisioning. There are well-known framings (expected value with asymmetric loss, multi-armed bandit, risk-sensitive utility). One of them is probably what you want.

### CLI contract

A single entry point:

```bash
python engine.py decide --incident eval/E01.json \
                        --history incidents_history.json \
                        --actions actions.yaml
```

Prints the engine's decision as a JSON document to stdout and appends one line to `audit.jsonl`.

### Audit log format (this is the grading contract — match it)

Each line in `audit.jsonl` must be a JSON object with **at minimum** these fields:

```json
{
  "incident_id":     "E01",                                  // required, must match eval file basename
  "selected_action": "rollback_service",                     // required, must be a name from actions.yaml
  "params":          {"service": "payment-svc"},             // required if the action takes parameters
  "confidence":      0.72,                                   // required, your engine's confidence 0..1
  "evidence":        { ... }                                 // free-form, see §4 Option B for what helps reviewers
}
```

You may add any other fields you find useful. The auto-grader reads only the first three. Manual review of `FINDINGS.md` consults `evidence`.

### Required deliverables

- A repository directory with your code, audit log, and findings document.
- Engine entry point named `engine.py` exposing the CLI above.
- `audit.jsonl` containing one entry per eval incident E01 through E08.
- `FINDINGS.md` answering the questions in §5.
- A `README.md` paragraph in your repo: how to set up + how to run.

How you split the code across files is your call.

---

## 4. What you may build — OPTIONAL

Pick any subset. None of these is required for a passing grade; each is worth bonus.

### Option A — Out-of-distribution detection

When the input incident is novel (nothing in the corpus closely resembles it), an auto-acting engine causes harm. Add an explicit OOD check that flags such inputs and changes the engine's behaviour.

**Defend** in FINDINGS: how you measure novelty, where you set the threshold, and how you validated that the threshold is neither too loose (false alarms) nor too tight (silent overconfidence).

### Option B — Justification chain

A reviewer reading your `audit.jsonl` should be able to verify the engine's decision from the evidence alone. Add a structured `evidence` block in each audit entry that traces the decision: which historical incidents voted, which signals pushed it that way, which alternatives were rejected and on what grounds.

**Defend** in FINDINGS: which evidence you chose to include, which you omitted, and why.

### Option C — Confidence calibration

If your engine outputs a confidence of 0.8, does it actually succeed 80% of the time? Run all 8 eval incidents and produce a reliability diagram (predicted confidence binned vs. actual hit rate).

**Defend** in FINDINGS: where your engine is over- or under-confident, and one concrete mitigation you tried (or rejected). Calibration techniques in §9.

### Option D — Adversarial robustness test

Hand-craft 3 incidents designed to break naive approaches: a novel pattern, an evidence-spoof (logs lie, traces tell truth), and an evidence-thin case (only a handful of relevant log lines). Run your engine on each.

**Defend** in FINDINGS: what failed, what held up, and what your engine's failure mode tells you about the design choice that caused it.
## 5. `FINDINGS.md` — required reflection questions

Your FINDINGS document must answer all five. Each answer must reference concrete numbers or behaviors from your own runs.

1. **Which similarity function did you choose for Layer 2, and why?** Reference at least one alternative you considered and an empirical reason for choosing the one you did.
2. **How does outcome-weighted voting change the candidate ranking versus a pure-similarity ranking?** Demonstrate with a concrete eval incident.
3. **For one eval incident, explain the EV calculation in full** — the candidate set, weights, P_success values, costs, and which action won and by how much.
4. **When did your engine choose to escalate (page_oncall) instead of auto-act?** Was that choice correct against the eval ground truth?
5. **What is the most likely class of incident that breaks your engine?** Propose one concrete improvement that would help, but explain why you did not implement it within the time budget.

---

## 6. Grading rubric (100 points)

| Item | Weight |
|---|---|
| Engine runs on all 8 eval incidents without error | 10 |
| Layer 1 features include genuine log + trace signals (not just metric) | 10 |
| Layer 2 retrieval + outcome-weighted voting implemented correctly | 15 |
| Layer 3 utility computation + blast-radius gate + edge cases handled | 15 |
| ≥5 of 8 eval incidents recommend an action in `accepted_actions` | 15 |
| No eval incident triggers a `must_not_action` (e.g., paging when auto-action is correct, or auto-acting on E05) | 10 |
| `audit.jsonl` contains an entry for every eval incident | 10 |
| `FINDINGS.md` answers all 5 questions with concrete references | 15 |
| Optional sections (A-D) | up to 20 bonus |

Tier thresholds: ≥85 excellent, ≥70 pass, ≥55 needs revision.

`FINDINGS.md` is weighted highest after the algorithmic layers — answers that could apply to any submission (no incident IDs, no numbers, no per-case references from your own run) lose half-credit on that question.

---

## 7. Submission

Submit a directory containing:

```
your-name/
├── engine.py
├── features.py
├── retrieval.py
├── decision.py
├── actions.yaml            (provided, may be modified — note changes in FINDINGS)
├── audit.jsonl             (your engine's decisions on all 8 eval incidents)
├── FINDINGS.md
├── (any optional artifacts: plots, adversarial JSONs, calibration code)
└── README.md               (one paragraph: how to run, expected output)
```

A reviewer should be able to clone your directory, run `python engine.py decide --incident eval/E01.json` (and the other seven) against the provided eval set, and reproduce your `audit.jsonl` exactly.

---

## 8. Out of scope

- You do **not** need to build a streaming pipeline, message queue, or distributed system. A single-process Python CLI is sufficient.
- You do **not** need to deploy to cloud or use containers. Local execution is sufficient.
- You do **not** need a UI. JSON output to stdout or a file is sufficient.
- You do **not** need to implement actual remediation. Returning a recommended action JSON is the deliverable; nothing executes.
- You do **not** need to handle real-time updates or incremental learning. Each decision is independent.

---

## 9. Reference reading (optional)

- Resnick, Iacovou, Suchak, Bergstrom, Riedl. *GroupLens: An open architecture for collaborative filtering of netnews.* (Foundation for similarity-based retrieval.)
- Howard, Matheson. *Influence Diagrams.* (Decision-theoretic framing of action selection under uncertainty.)
- Drain log parsing — He et al. *Drain: An Online Log Parsing Approach with Fixed Depth Tree.* ICWS 2017.

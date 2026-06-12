# Evidence-Driven Remediation Engine

This directory contains a complete local solution for the lab. It implements three layers: feature extraction from logs/traces/metrics (`features.py`), precedent retrieval and outcome-weighted voting (`retrieval.py`), and cost/blast-radius-aware action selection (`decision.py`). The CLI entry point is `engine.py`.

## Setup

Use Python 3.10 or newer. The only non-standard dependency used by the engine is PyYAML:

```bash
pip install pyyaml
```

## Run One Incident

From this `data-pack` directory:

```bash
python engine.py decide --incident eval/E01.json --history incidents_history.json --actions actions.yaml
```

The command prints a JSON decision to stdout and appends the same decision to `audit.jsonl`.

## Regenerate Audit For All Eval Incidents

If you want a fresh audit file, delete `audit.jsonl` first, then run:

```powershell
Remove-Item audit.jsonl -ErrorAction SilentlyContinue
foreach ($i in 1..8) {
  $id = "E{0:D2}" -f $i
  python engine.py decide --incident "eval/$id.json" --history incidents_history.json --actions actions.yaml
}
```

## Grade

```bash
python grade.py --audit audit.jsonl --expected eval/expected.json
```

Current generated result:

```text
Correct: 8/8
Forbidden (chose must_not_action): 0/8
Missing from audit: 0/8
```

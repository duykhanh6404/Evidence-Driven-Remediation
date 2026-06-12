"""Auto-grade a student's audit.jsonl against eval/expected.json.

Usage:
    python grade.py --audit audit.jsonl --expected eval/expected.json
"""
import argparse
import json
import sys
from pathlib import Path


def action_matches(recommended: dict, accepted: dict) -> bool:
    """Match by action name AND any specified params (subset match)."""
    if recommended.get("selected_action") != accepted.get("name"):
        return False
    accepted_params = accepted.get("params", {}) or {}
    rec_params = recommended.get("params", {}) or {}
    for k, v in accepted_params.items():
        if rec_params.get(k) != v:
            return False
    return True


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--audit", required=True)
    p.add_argument("--expected", required=True)
    args = p.parse_args()

    expected = json.loads(Path(args.expected).read_text())
    audit_lines = [json.loads(line) for line in Path(args.audit).read_text().splitlines() if line.strip()]
    by_id = {e.get("incident_id"): e for e in audit_lines}

    correct = 0
    forbidden = 0
    missing = 0
    detail = []

    for eid, expected_entry in expected.items():
        rec = by_id.get(eid)
        if rec is None:
            missing += 1
            detail.append((eid, "MISSING from audit.jsonl"))
            continue
        accepted = expected_entry.get("accepted_actions", [])
        must_not = expected_entry.get("must_not_action")
        if must_not and rec.get("selected_action") == must_not:
            forbidden += 1
            detail.append((eid, f"VIOLATED must_not_action ({must_not})"))
            continue
        if any(action_matches(rec, a) for a in accepted):
            correct += 1
            detail.append((eid, f"OK -> {rec['selected_action']}"))
        else:
            detail.append((eid, f"WRONG -> {rec['selected_action']}; expected one of {[a['name'] for a in accepted]}"))

    total = len(expected)
    print(f"Correct: {correct}/{total}")
    print(f"Forbidden (chose must_not_action): {forbidden}/{total}")
    print(f"Missing from audit: {missing}/{total}")
    print()
    print("Per-incident detail:")
    for eid, note in detail:
        print(f"  {eid}: {note}")

    # Rubric estimate
    print()
    runs_ok = (missing == 0)
    score = 0
    if runs_ok: score += 10
    # 10 pts feature mix + 15 retrieval + 15 decision (these are code-review, default give 30 if audit looks legit)
    if audit_lines:
        sample = audit_lines[0]
        if sample.get("top_3_neighbors"): score += 15
        if sample.get("consensus_score") is not None: score += 15
        if all("blast_radius_check" in e or "blast_radius_services" in e.get("selected_action_meta", {}) for e in audit_lines[:1]): score += 10
    pct_correct = correct / total
    if pct_correct >= 0.6: score += 15
    elif pct_correct >= 0.4: score += 10
    if forbidden == 0: score += 10
    elif forbidden <= 1: score += 5
    if missing == 0: score += 10
    print(f"Auto-rubric estimate (excluding FINDINGS + features review): {score}/85")
    print("FINDINGS (15 pts) + optional bonus (up to 20) graded manually.")
    return 0 if (missing == 0 and forbidden <= 1) else 1


if __name__ == "__main__":
    sys.exit(main())

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import yaml

from decision import select_action
from features import extract_features
from retrieval import retrieve_and_vote


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_yaml(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def decide(incident_path: Path, history_path: Path, actions_path: Path) -> dict[str, Any]:
    incident = load_json(incident_path)
    history = load_json(history_path)
    actions_catalog = load_yaml(actions_path)

    query = extract_features(incident)
    retrieval = retrieve_and_vote(query, history, actions_catalog=actions_catalog)
    decision = select_action(retrieval, actions_catalog)
    return decision


def append_audit(decision: dict[str, Any], audit_path: Path) -> None:
    with audit_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(decision, ensure_ascii=False) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Evidence-driven remediation engine.")
    sub = parser.add_subparsers(dest="command")

    decide_parser = sub.add_parser("decide")
    decide_parser.add_argument("--incident", required=True, type=Path)
    decide_parser.add_argument("--history", default=Path("incidents_history.json"), type=Path)
    decide_parser.add_argument("--actions", default=Path("actions.yaml"), type=Path)
    decide_parser.add_argument("--audit", default=Path("audit.jsonl"), type=Path)

    args = parser.parse_args()
    if args.command != "decide":
        parser.print_help()
        return 1

    decision = decide(args.incident, args.history, args.actions)
    print(json.dumps(decision, indent=2, ensure_ascii=False))
    append_audit(decision, args.audit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

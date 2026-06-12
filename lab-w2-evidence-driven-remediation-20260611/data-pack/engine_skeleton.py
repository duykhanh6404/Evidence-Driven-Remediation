"""Optional starting skeleton — feel free to ignore and write from scratch.

This is NOT a working solution. It outlines the function signatures.
"""
import argparse
import json
import yaml
from pathlib import Path


def extract_features(incident: dict) -> dict:
    """Layer 1: pull log + trace + metric features into an incident_vector."""
    raise NotImplementedError("Implement in features.py")


def similarity(a: dict, b: dict) -> float:
    """Layer 2 helper: similarity between two incident_vectors."""
    raise NotImplementedError("Implement in retrieval.py")


def retrieve_and_vote(query: dict, history: list[dict], top_k: int = 3) -> dict:
    """Layer 2: kNN over history + outcome-weighted action voting."""
    raise NotImplementedError("Implement in retrieval.py")


def select_action(candidates: dict, actions_catalog: list[dict]) -> dict:
    """Layer 3: cost-aware utility + blast-radius gate."""
    raise NotImplementedError("Implement in decision.py")


def decide(incident_path: Path, history_path: Path, actions_path: Path) -> dict:
    incident = json.loads(incident_path.read_text())
    history = json.loads(history_path.read_text())
    actions_catalog = yaml.safe_load(actions_path.read_text())
    vec = extract_features(incident)
    candidates = retrieve_and_vote(vec, history)
    decision = select_action(candidates, actions_catalog)
    return decision


def main() -> int:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd")
    d = sub.add_parser("decide")
    d.add_argument("--incident", required=True)
    d.add_argument("--history", default="incidents_history.json")
    d.add_argument("--actions", default="actions.yaml")
    args = p.parse_args()
    if args.cmd == "decide":
        out = decide(Path(args.incident), Path(args.history), Path(args.actions))
        print(json.dumps(out, indent=2))
        with open("audit.jsonl", "a") as f:
            f.write(json.dumps(out) + "\n")
        return 0
    p.print_help()
    return 1


if __name__ == "__main__":
    import sys
    sys.exit(main())

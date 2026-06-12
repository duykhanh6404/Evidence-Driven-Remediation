from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from features import extract_features, extract_history_corpus_features


OUTCOME_WEIGHTS = {
    "success": 1.0,
    "partial": 0.45,
    "failed": -0.75,
}

SIGNAL_WEIGHTS = {
    "log": 0.38,
    "trace": 0.34,
    "service": 0.18,
    "metric": 0.10,
}

DEFAULT_TOP_K = 5
DEFAULT_MIN_SIMILARITY = 0.18
DEFAULT_OOD_THRESHOLD = 0.28


def dot(a: dict[str, float], b: dict[str, float]) -> float:
    if len(a) > len(b):
        a, b = b, a
    return sum(float(value) * float(b.get(key, 0.0)) for key, value in a.items())


def norm(a: dict[str, float]) -> float:
    return math.sqrt(sum(float(value) ** 2 for value in a.values()))


def cosine_similarity(a: dict[str, float], b: dict[str, float]) -> float:
    denom = norm(a) * norm(b)
    if denom <= 0:
        return 0.0
    return max(0.0, min(1.0, dot(a, b) / denom))


def jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 0.0
    union = a | b
    return len(a & b) / len(union) if union else 0.0


def edge_signature(edge: dict[str, Any], directed: bool = True) -> str:
    src, dst = edge.get("from"), edge.get("to")
    if directed:
        return f"{src}->{dst}"
    return "--".join(sorted([str(src), str(dst)]))


def trace_edge_similarity(query: dict[str, Any], history: dict[str, Any]) -> float:
    q_edges = {
        edge_signature(edge): float(edge.get("score", 0.0))
        for edge in query.get("trace", {}).get("anomalous_edges", [])
    }
    h_edges = {
        edge_signature(edge): float(edge.get("score", 0.0))
        for edge in history.get("trace", {}).get("anomalous_edges", [])
    }
    directed = cosine_similarity(q_edges, h_edges)

    q_undirected = {
        edge_signature(edge, directed=False): float(edge.get("score", 0.0))
        for edge in query.get("trace", {}).get("anomalous_edges", [])
    }
    h_undirected = {
        edge_signature(edge, directed=False): float(edge.get("score", 0.0))
        for edge in history.get("trace", {}).get("anomalous_edges", [])
    }
    undirected = cosine_similarity(q_undirected, h_undirected)

    # Some historical signatures encode dependency direction inconsistently.
    # Keep directed evidence strongest, but let an undirected match count.
    return max(directed, 0.75 * undirected)


def similarity(query: dict[str, Any], history: dict[str, Any]) -> dict[str, float]:
    """Hybrid similarity between two Layer 1 vectors."""
    q_tokens = query.get("tokens", {})
    h_tokens = history.get("tokens", {})
    log_sim = cosine_similarity(q_tokens.get("log", {}), h_tokens.get("log", {}))
    trace_token_sim = cosine_similarity(q_tokens.get("trace", {}), h_tokens.get("trace", {}))
    trace_edge_sim = trace_edge_similarity(query, history)
    trace_sim = max(trace_token_sim, trace_edge_sim)
    service_sim = jaccard(set(query.get("affected_services", [])), set(history.get("affected_services", [])))
    metric_sim = cosine_similarity(q_tokens.get("metric", {}), h_tokens.get("metric", {}))

    total = (
        SIGNAL_WEIGHTS["log"] * log_sim
        + SIGNAL_WEIGHTS["trace"] * trace_sim
        + SIGNAL_WEIGHTS["service"] * service_sim
        + SIGNAL_WEIGHTS["metric"] * metric_sim
    )
    # Reward cases where both mandatory evidence families agree.
    if log_sim >= 0.25 and trace_sim >= 0.25:
        total += 0.05
    return {
        "total": round(min(1.0, total), 4),
        "log": round(log_sim, 4),
        "trace": round(trace_sim, 4),
        "trace_tokens": round(trace_token_sim, 4),
        "trace_edges": round(trace_edge_sim, 4),
        "service": round(service_sim, 4),
        "metric": round(metric_sim, 4),
    }


def action_catalog_params(actions_catalog: list[dict[str, Any]] | None) -> dict[str, list[str]]:
    if not actions_catalog:
        return {
            "rollback_service": ["service", "target_version"],
            "increase_pool_size": ["service", "from_value", "to_value"],
            "restart_pod": ["service", "pod_selector"],
            "dns_config_rollback": ["configmap_name", "target_revision"],
            "network_policy_revert": ["policy_name"],
            "page_oncall": ["team"],
        }
    return {
        item["name"]: list(item.get("params", []))
        for item in actions_catalog
        if item.get("name")
    }


def parse_history_action(action: str, actions_catalog: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    parts = (action or "").split(":")
    name = parts[0] if parts and parts[0] else "page_oncall"
    raw_values = parts[1:]
    param_names = action_catalog_params(actions_catalog).get(name, [])
    params = {}
    for idx, value in enumerate(raw_values):
        key = param_names[idx] if idx < len(param_names) else f"arg{idx + 1}"
        params[key] = value

    if name == "rollback_service" and "target_version" not in params:
        params["target_version"] = "previous"
    if name == "page_oncall" and "team" not in params:
        params["team"] = "platform-team"
    return {"name": name, "params": params}


def query_service_scores(query: dict[str, Any]) -> dict[str, float]:
    """Rank current-incident services using only query evidence."""
    scores: Counter[str] = Counter()
    affected = query.get("affected_services", [])
    for idx, service in enumerate(affected):
        scores[service] += max(0.1, 1.0 - (idx * 0.05))

    for service, count in query.get("log", {}).get("service_signal_counts", {}).items():
        scores[service] += min(4.0, float(count) / 40.0)
    for service, value in query.get("trace", {}).get("service_signal_counts", {}).items():
        scores[service] += 2.5 * float(value)
    for service, value in query.get("metric", {}).get("service_signal_counts", {}).items():
        scores[service] += 0.75 * float(value)

    return {service: round(score, 4) for service, score in scores.items()}


def retarget_action_to_query(action: dict[str, Any], query: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Move a historical service-specific action onto the closest query service.

    This is not a class-to-action rule. It only adapts a parameter from a
    precedent when that exact historical service is absent from the live
    incident evidence.
    """
    params = dict(action.get("params", {}))
    old_service = params.get("service")
    if not old_service:
        return action, None

    affected = set(query.get("affected_services", []))
    if old_service in affected:
        return {"name": action["name"], "params": params}, None

    service_scores = query_service_scores(query)
    if not service_scores:
        return {"name": action["name"], "params": params}, None

    new_service, score = max(service_scores.items(), key=lambda item: item[1])
    params["service"] = new_service
    if action.get("name") == "rollback_service":
        params.setdefault("target_version", "previous")

    return (
        {"name": action["name"], "params": params},
        {
            "from_service": old_service,
            "to_service": new_service,
            "reason": "historical service absent from query affected_services",
            "query_service_score": score,
        },
    )


def action_key(action: dict[str, Any]) -> str:
    params = action.get("params", {})
    # Candidate identity keeps service-specific actions separate while allowing
    # versions/selectors to be chosen by the final decision layer.
    service = params.get("service")
    if service:
        return f"{action.get('name')}|service={service}"
    if action.get("name") == "page_oncall":
        return "page_oncall"
    stable = ",".join(f"{key}={params[key]}" for key in sorted(params))
    return f"{action.get('name')}|{stable}"


def merge_action_params(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    merged = dict(existing)
    for key, value in incoming.items():
        if key not in merged or merged[key] in {"", "unknown", "previous", "default"}:
            merged[key] = value
    return merged


def retrieval_rank(
    query: dict[str, Any],
    history_vectors: list[dict[str, Any]],
    top_k: int = DEFAULT_TOP_K,
) -> list[dict[str, Any]]:
    rows = []
    for hist in history_vectors:
        sims = similarity(query, hist)
        rows.append(
            {
                "incident_id": hist.get("incident_id"),
                "root_cause_class": hist.get("root_cause_class"),
                "outcome": hist.get("outcome"),
                "actions_taken": hist.get("actions_taken", []),
                "affected_services": hist.get("affected_services", []),
                "similarity": sims["total"],
                "components": sims,
                "evidence_summary": hist.get("evidence_summary", {}),
            }
        )
    rows.sort(key=lambda row: row["similarity"], reverse=True)
    return rows[:top_k]


def vote_actions(
    query: dict[str, Any],
    neighbors: list[dict[str, Any]],
    actions_catalog: list[dict[str, Any]] | None = None,
    min_similarity: float = DEFAULT_MIN_SIMILARITY,
) -> list[dict[str, Any]]:
    votes: dict[str, dict[str, Any]] = {}

    for neighbor in neighbors:
        sim = float(neighbor.get("similarity", 0.0))
        if sim < min_similarity:
            continue
        outcome = neighbor.get("outcome", "partial")
        outcome_weight = OUTCOME_WEIGHTS.get(outcome, 0.0)
        if outcome_weight == 0:
            continue
        seen_in_neighbor = set()
        for raw_action in neighbor.get("actions_taken", []):
            action = parse_history_action(raw_action, actions_catalog)
            action, retarget = retarget_action_to_query(action, query)
            key = action_key(action)
            if key in seen_in_neighbor:
                continue
            seen_in_neighbor.add(key)

            contribution = sim * outcome_weight
            if key not in votes:
                votes[key] = {
                    "action": {"name": action["name"], "params": dict(action.get("params", {}))},
                    "positive_vote": 0.0,
                    "negative_vote": 0.0,
                    "net_vote": 0.0,
                    "support_count": 0,
                    "against_count": 0,
                    "evidence": [],
                }
            slot = votes[key]
            slot["action"]["params"] = merge_action_params(
                slot["action"].get("params", {}), action.get("params", {})
            )
            slot["net_vote"] += contribution
            if contribution >= 0:
                slot["positive_vote"] += contribution
                slot["support_count"] += 1
            else:
                slot["negative_vote"] += abs(contribution)
                slot["against_count"] += 1
            slot["evidence"].append(
                {
                    "incident_id": neighbor.get("incident_id"),
                    "similarity": round(sim, 4),
                    "outcome": outcome,
                    "outcome_weight": outcome_weight,
                    "contribution": round(contribution, 4),
                    "components": neighbor.get("components", {}),
                    "retarget": retarget,
                }
            )

    candidates = []
    for key, vote in votes.items():
        positive = float(vote["positive_vote"])
        negative = float(vote["negative_vote"])
        support_mass = positive + negative
        confidence = positive / support_mass if support_mass > 0 else 0.0
        # Net score decides ranking; confidence stays separate for Layer 3.
        candidate = {
            "key": key,
            "action": vote["action"],
            "score": round(vote["net_vote"], 4),
            "positive_vote": round(positive, 4),
            "negative_vote": round(negative, 4),
            "confidence": round(confidence, 4),
            "support_count": vote["support_count"],
            "against_count": vote["against_count"],
            "evidence": sorted(
                vote["evidence"],
                key=lambda row: abs(float(row["contribution"])),
                reverse=True,
            ),
        }
        candidates.append(candidate)

    candidates.sort(
        key=lambda item: (
            item["score"],
            item["confidence"],
            item["positive_vote"],
            -item["negative_vote"],
        ),
        reverse=True,
    )
    return candidates


def retrieve_and_vote(
    query: dict[str, Any],
    history: list[dict[str, Any]],
    top_k: int = DEFAULT_TOP_K,
    min_similarity: float = DEFAULT_MIN_SIMILARITY,
    ood_threshold: float = DEFAULT_OOD_THRESHOLD,
    actions_catalog: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Layer 2: retrieve similar incidents and derive candidate actions."""
    history_vectors = []
    for entry in history:
        if entry.get("kind") == "history" and "tokens" in entry:
            history_vectors.append(entry)
        else:
            history_vectors.extend(extract_history_corpus_features([entry]))

    neighbors = retrieval_rank(query, history_vectors, top_k=top_k)
    relevant = [row for row in neighbors if row["similarity"] >= min_similarity]
    candidates = vote_actions(
        query, relevant, actions_catalog=actions_catalog, min_similarity=min_similarity
    )
    best_similarity = neighbors[0]["similarity"] if neighbors else 0.0

    no_precedent = best_similarity < ood_threshold or not relevant
    if no_precedent:
        candidates = [
            {
                "key": "page_oncall",
                "action": {"name": "page_oncall", "params": {"team": "platform-team"}},
                "score": 0.0,
                "positive_vote": 0.0,
                "negative_vote": 0.0,
                "confidence": round(max(0.0, 1.0 - best_similarity), 4),
                "support_count": 0,
                "against_count": 0,
                "evidence": [],
                "reason": "no_close_precedent",
            }
        ]

    return {
        "query_incident_id": query.get("incident_id"),
        "top_k": top_k,
        "min_similarity": min_similarity,
        "ood_threshold": ood_threshold,
        "best_similarity": round(best_similarity, 4),
        "no_precedent": no_precedent,
        "neighbors": neighbors,
        "relevant_neighbors": relevant,
        "candidates": candidates,
        "evidence": {
            "retrieval_method": "weighted hybrid cosine+jaccard over log, trace, service, metric signals",
            "signal_weights": SIGNAL_WEIGHTS,
            "outcome_weights": OUTCOME_WEIGHTS,
            "query_top_logs": query.get("evidence_summary", {}).get("top_log_templates", [])[:3],
            "query_top_traces": query.get("evidence_summary", {}).get("top_trace_edges", [])[:3],
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect Layer 2 retrieval and action voting.")
    parser.add_argument("--incident", type=Path, required=True)
    parser.add_argument("--history", type=Path, default=Path("incidents_history.json"))
    parser.add_argument("--actions", type=Path)
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    args = parser.parse_args()

    incident = json.loads(args.incident.read_text(encoding="utf-8"))
    history = json.loads(args.history.read_text(encoding="utf-8"))
    actions_catalog = None
    if args.actions:
        import yaml

        actions_catalog = yaml.safe_load(args.actions.read_text(encoding="utf-8"))

    query = extract_features(incident)
    result = retrieve_and_vote(query, history, top_k=args.top_k, actions_catalog=actions_catalog)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

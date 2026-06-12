from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


PAGE_ACTION = "page_oncall"


def catalog_by_name(actions_catalog: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {item["name"]: item for item in actions_catalog if item.get("name")}


def selected_params(action: dict[str, Any]) -> dict[str, Any]:
    params = dict(action.get("params", {}) or {})
    if action.get("name") == "rollback_service":
        params.setdefault("target_version", "previous")
    if action.get("name") == PAGE_ACTION:
        params.setdefault("team", "platform-team")
    return params


def positive_candidate_mass(candidates: list[dict[str, Any]]) -> float:
    return sum(max(0.0, float(candidate.get("score", 0.0))) for candidate in candidates)


def query_trace_services(retrieval_result: dict[str, Any], limit: int = 2) -> set[str]:
    services: set[str] = set()
    traces = retrieval_result.get("evidence", {}).get("query_top_traces", [])[:limit]
    for edge in traces:
        if edge.get("from"):
            services.add(edge["from"])
        if edge.get("to"):
            services.add(edge["to"])
    return services


def query_log_services(retrieval_result: dict[str, Any], limit: int = 3) -> set[str]:
    services: set[str] = set()
    logs = retrieval_result.get("evidence", {}).get("query_top_logs", [])[:limit]
    for template in logs:
        for row in template.get("services", []):
            if row.get("service"):
                services.add(row["service"])
    return services


def evidence_alignment(candidate: dict[str, Any], retrieval_result: dict[str, Any]) -> dict[str, Any]:
    action = candidate.get("action", {})
    service = selected_params(action).get("service")
    trace_services = query_trace_services(retrieval_result)
    log_services = query_log_services(retrieval_result)
    if not service:
        return {"factor": 1.0, "reason": "action has no service parameter"}
    if service in trace_services:
        return {
            "factor": 1.0,
            "reason": "action service appears in top trace evidence",
            "service": service,
        }
    if trace_services and service in log_services:
        return {
            "factor": 0.65,
            "reason": "logs support service but top trace evidence points elsewhere",
            "service": service,
            "trace_services": sorted(trace_services),
        }
    if service in log_services:
        return {
            "factor": 0.85,
            "reason": "action service appears in top log evidence",
            "service": service,
        }
    if trace_services:
        return {
            "factor": 0.55,
            "reason": "action service is absent from strongest trace evidence",
            "service": service,
            "trace_services": sorted(trace_services),
        }
    return {
        "factor": 0.75,
        "reason": "service-specific action has weak direct evidence",
        "service": service,
    }


def candidate_vote_share(candidate: dict[str, Any], candidates: list[dict[str, Any]]) -> float:
    total = positive_candidate_mass(candidates)
    if total <= 0:
        return 0.0
    return max(0.0, float(candidate.get("score", 0.0))) / total


def calibrate_confidence(
    candidate: dict[str, Any],
    retrieval_result: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    if candidate.get("action", {}).get("name") == PAGE_ACTION and retrieval_result.get("no_precedent"):
        confidence = float(candidate.get("confidence", 0.0))
        return {
            "confidence": round(min(1.0, max(0.0, confidence)), 4),
            "vote_share": 0.0,
            "precedent_strength": 0.0,
            "alignment": {"factor": 1.0, "reason": "no close precedent"},
        }

    vote_confidence = max(0.0, min(1.0, float(candidate.get("confidence", 0.0))))
    precedent_strength = min(1.0, float(retrieval_result.get("best_similarity", 0.0)) / 0.75)
    vote_share = candidate_vote_share(candidate, candidates)
    support_bonus = min(0.08, 0.025 * int(candidate.get("support_count", 0)))
    against_penalty = min(0.20, 0.08 * int(candidate.get("against_count", 0)))
    raw = (
        0.45 * vote_confidence
        + 0.30 * precedent_strength
        + 0.25 * vote_share
        + support_bonus
        - against_penalty
    )
    alignment = evidence_alignment(candidate, retrieval_result)
    confidence = min(1.0, max(0.0, raw * alignment["factor"]))
    return {
        "confidence": round(confidence, 4),
        "vote_share": round(vote_share, 4),
        "precedent_strength": round(precedent_strength, 4),
        "alignment": alignment,
    }


def risk_threshold(meta: dict[str, Any]) -> float:
    blast = float(meta.get("blast_radius_services", 0) or 0)
    downtime = float(meta.get("downtime_min", 0) or 0)
    return min(0.9, 0.50 + (0.08 * blast) + (0.02 * downtime))


def action_cost_penalty(meta: dict[str, Any]) -> float:
    cost = float(meta.get("cost_min", 0) or 0)
    downtime = float(meta.get("downtime_min", 0) or 0)
    blast = float(meta.get("blast_radius_services", 0) or 0)
    rollback_window = float(meta.get("rollback_window_sec", 0) or 0)
    return (0.015 * cost) + (0.03 * downtime) + (0.08 * blast) + (0.0005 * rollback_window)


def action_utility(confidence: float, meta: dict[str, Any], is_page: bool) -> float:
    if is_page:
        # Page has zero catalog cost, but it is operationally expensive. Keep it
        # as the fallback, not the default winner when auto-action evidence is strong.
        return 0.38 + (0.40 * (1.0 - confidence))
    blast = float(meta.get("blast_radius_services", 0) or 0)
    downside = (1.0 - confidence) * (0.22 + 0.18 * blast)
    return (1.35 * confidence) - action_cost_penalty(meta) - downside


def evaluate_candidate(
    candidate: dict[str, Any],
    retrieval_result: dict[str, Any],
    actions_by_name: dict[str, dict[str, Any]],
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    action = candidate.get("action", {})
    name = action.get("name")
    meta = actions_by_name.get(name, {})
    calibrated = calibrate_confidence(candidate, retrieval_result, candidates)
    confidence = calibrated["confidence"]
    threshold = risk_threshold(meta)
    is_page = name == PAGE_ACTION
    allowed = is_page or confidence >= threshold
    if not is_page and retrieval_result.get("no_precedent"):
        allowed = False
    vote_strength = min(2.0, max(0.0, float(candidate.get("score", 0.0))))
    utility = action_utility(confidence, meta, is_page)
    if not is_page:
        utility += 0.25 * vote_strength
        utility += 0.06 * min(3, int(candidate.get("support_count", 0)))
    return {
        "candidate": candidate,
        "name": name,
        "params": selected_params(action),
        "confidence": confidence,
        "utility": round(utility, 4),
        "allowed": allowed,
        "risk_threshold": round(threshold, 4),
        "meta": meta,
        "calibration": calibrated,
        "vote_strength": round(vote_strength, 4),
        "blocked_reason": None
        if allowed
        else "confidence_below_blast_radius_threshold"
        if not retrieval_result.get("no_precedent")
        else "no_close_precedent",
    }


def fallback_page(actions_by_name: dict[str, dict[str, Any]], confidence: float, reason: str) -> dict[str, Any]:
    meta = actions_by_name.get(PAGE_ACTION, {})
    return {
        "candidate": {
            "key": PAGE_ACTION,
            "action": {"name": PAGE_ACTION, "params": {"team": "platform-team"}},
            "score": 0.0,
            "confidence": confidence,
            "evidence": [],
            "reason": reason,
        },
        "name": PAGE_ACTION,
        "params": {"team": "platform-team"},
        "confidence": round(confidence, 4),
        "utility": round(action_utility(confidence, meta, True), 4),
        "allowed": True,
        "risk_threshold": risk_threshold(meta),
        "meta": meta,
        "calibration": {
            "confidence": round(confidence, 4),
            "vote_share": 0.0,
            "precedent_strength": 0.0,
            "alignment": {"factor": 1.0, "reason": reason},
        },
        "blocked_reason": None,
    }


def incident_short_id(raw_id: str | None) -> str:
    if not raw_id:
        return "unknown"
    return raw_id.split("-", 1)[0]


def select_action(
    retrieval_result: dict[str, Any],
    actions_catalog: list[dict[str, Any]],
) -> dict[str, Any]:
    """Layer 3: choose final action from Layer 2 candidates."""
    actions_by_name = catalog_by_name(actions_catalog)
    candidates = retrieval_result.get("candidates", [])

    evaluations = [
        evaluate_candidate(candidate, retrieval_result, actions_by_name, candidates)
        for candidate in candidates
    ]
    if not evaluations:
        evaluations = [
            fallback_page(
                actions_by_name,
                confidence=max(0.5, 1.0 - float(retrieval_result.get("best_similarity", 0.0))),
                reason="no candidates returned by retrieval",
            )
        ]

    page_eval = next((item for item in evaluations if item["name"] == PAGE_ACTION), None)
    if page_eval is None:
        page_conf = max(0.35, 1.0 - float(retrieval_result.get("best_similarity", 0.0)))
        page_eval = fallback_page(actions_by_name, page_conf, "fallback escalation candidate")
        evaluations.append(page_eval)

    allowed = [item for item in evaluations if item["allowed"]]
    if retrieval_result.get("no_precedent"):
        selected = page_eval
        selected_reason = "no_close_precedent"
    elif allowed:
        selected = max(allowed, key=lambda item: item["utility"])
        selected_reason = "max_allowed_utility"
    else:
        selected = page_eval
        selected_reason = "all_auto_actions_blocked"

    top_neighbors = [
        {
            "incident_id": row.get("incident_id"),
            "similarity": row.get("similarity"),
            "outcome": row.get("outcome"),
            "root_cause_class": row.get("root_cause_class"),
        }
        for row in retrieval_result.get("neighbors", [])[:3]
    ]

    alternatives = []
    for item in sorted(evaluations, key=lambda row: row["utility"], reverse=True):
        alternatives.append(
            {
                "action": item["name"],
                "params": item["params"],
                "confidence": item["confidence"],
                "utility": item["utility"],
                "allowed": item["allowed"],
                "risk_threshold": item["risk_threshold"],
                "blocked_reason": item["blocked_reason"],
                "alignment": item["calibration"]["alignment"],
                "vote_strength": item.get("vote_strength", 0.0),
            }
        )

    output = {
        "incident_id": incident_short_id(retrieval_result.get("query_incident_id")),
        "selected_action": selected["name"],
        "params": selected["params"],
        "confidence": selected["confidence"],
        "consensus_score": round(
            selected["calibration"].get("vote_share", 0.0), 4
        ),
        "selected_action_meta": selected["meta"],
        "blast_radius_check": {
            "blast_radius_services": selected["meta"].get("blast_radius_services", 0),
            "risk_threshold": selected["risk_threshold"],
            "passed": selected["allowed"],
        },
        "selection_reason": selected_reason,
        "top_3_neighbors": top_neighbors,
        "candidate_utilities": alternatives,
        "evidence": {
            "best_similarity": retrieval_result.get("best_similarity"),
            "no_precedent": retrieval_result.get("no_precedent"),
            "retrieval_method": retrieval_result.get("evidence", {}).get("retrieval_method"),
            "signal_weights": retrieval_result.get("evidence", {}).get("signal_weights"),
            "outcome_weights": retrieval_result.get("evidence", {}).get("outcome_weights"),
            "selected_candidate_evidence": selected["candidate"].get("evidence", [])[:3],
            "query_top_logs": retrieval_result.get("evidence", {}).get("query_top_logs", [])[:3],
            "query_top_traces": retrieval_result.get("evidence", {}).get("query_top_traces", [])[:3],
        },
    }
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect Layer 3 action selection.")
    parser.add_argument("--retrieval-result", type=Path, required=True)
    parser.add_argument("--actions", type=Path, default=Path("actions.yaml"))
    args = parser.parse_args()

    import yaml

    retrieval_result = json.loads(args.retrieval_result.read_text(encoding="utf-8"))
    actions_catalog = yaml.safe_load(args.actions.read_text(encoding="utf-8"))
    print(json.dumps(select_action(retrieval_result, actions_catalog), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

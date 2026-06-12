from __future__ import annotations

import argparse
import json
import math
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any


VOLATILE_KEY_RE = re.compile(r"\b([a-zA-Z_][\w.-]*)=([^\s,;]+)")
UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
HEX_RE = re.compile(r"\b0x[0-9a-f]+\b", re.IGNORECASE)
NUMBER_RE = re.compile(r"\b\d+(?:\.\d+)?(?:ms|s|m|%|mb|gb)?\b", re.IGNORECASE)
TOKEN_RE = re.compile(r"[a-z][a-z0-9_-]*", re.IGNORECASE)
SERVICE_RE = re.compile(r"\b[a-z0-9][a-z0-9-]*(?:-svc|-db|-redis|-events|-edge|power|service|esb)\b")

STOP_TOKENS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "in",
    "is",
    "of",
    "on",
    "or",
    "the",
    "to",
    "with",
    "val",
    "num",
}


def parse_ts(value: str | None) -> datetime:
    if not value:
        return datetime.min.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def safe_ratio(after: float, before: float) -> float:
    if before == 0:
        return 1.0 if after == 0 else min(99.0, after)
    return after / before


def compact_float(value: float, digits: int = 4) -> float:
    if math.isnan(value) or math.isinf(value):
        return 0.0
    return round(float(value), digits)


def topology_index(topology: dict[str, Any] | None) -> dict[str, Any]:
    topology = topology or {}
    nodes = topology.get("nodes", [])
    edges = topology.get("edges", [])
    tiers = {node.get("id"): node.get("tier", "unknown") for node in nodes if node.get("id")}
    protocols = {}
    incoming: dict[str, set[str]] = defaultdict(set)
    outgoing: dict[str, set[str]] = defaultdict(set)
    services = set(tiers)

    for edge in edges:
        src, dst = edge.get("from"), edge.get("to")
        if not src or not dst:
            continue
        services.add(src)
        services.add(dst)
        protocols[(src, dst)] = edge.get("protocol", "unknown")
        outgoing[src].add(dst)
        incoming[dst].add(src)

    return {
        "tiers": tiers,
        "protocols": protocols,
        "incoming": incoming,
        "outgoing": outgoing,
        "services": services,
    }


def normalize_log_message(message: str) -> str:
    """Convert a raw log line into a stable, history-comparable template."""
    text = (message or "").strip().lower()
    text = UUID_RE.sub("<id>", text)
    text = HEX_RE.sub("<id>", text)
    text = re.sub(r"https?://\S+", "<url>", text)

    def repl_key(match: re.Match[str]) -> str:
        key = match.group(1).lower()
        value = match.group(2)
        if re.fullmatch(r"[\d.]+(?:ms|s|m|%|mb|gb)?", value, re.IGNORECASE):
            return f"{key}=num"
        if key.endswith("id") or key in {"attempt", "retries", "trace", "span"}:
            return f"{key}=id"
        return f"{key}=val"

    text = VOLATILE_KEY_RE.sub(repl_key, text)
    text = NUMBER_RE.sub("num", text)
    text = re.sub(r"[/._:(),;>\[\]{}]+", " ", text)
    text = text.replace("<", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def tokenize_text(text: str) -> list[str]:
    tokens = []
    for token in TOKEN_RE.findall(normalize_log_message(text)):
        token = token.lower()
        if token not in STOP_TOKENS and len(token) > 1:
            tokens.append(token)
    return tokens


def weighted_tokens_from_counter(counter: Counter[str], weight: float = 1.0) -> dict[str, float]:
    return {key: compact_float(value * weight) for key, value in sorted(counter.items())}


def extract_log_features(
    logs: list[dict[str, Any]],
    known_services: set[str] | None = None,
    top_n: int = 20,
) -> dict[str, Any]:
    known_services = known_services or set()
    template_counts: Counter[str] = Counter()
    token_counts: Counter[str] = Counter()
    service_counts: Counter[str] = Counter()
    service_signal_counts: Counter[str] = Counter()
    level_counts: Counter[str] = Counter()
    template_services: dict[str, Counter[str]] = defaultdict(Counter)
    mentioned_services: Counter[str] = Counter()

    for row in logs:
        service = row.get("svc") or row.get("service") or "unknown"
        level = (row.get("level") or "INFO").upper()
        message = row.get("msg") or row.get("message") or ""
        template = normalize_log_message(message)
        severity_weight = 3 if level == "ERROR" else 2 if level == "WARN" else 0.5

        service_counts[service] += 1
        level_counts[level] += 1
        template_counts[template] += 1
        template_services[template][service] += 1

        if level in {"ERROR", "WARN", "FATAL", "CRITICAL"}:
            service_signal_counts[service] += 1
            for token in tokenize_text(message):
                token_counts[token] += severity_weight

        candidates = set(SERVICE_RE.findall(message.lower()))
        candidates.update(s for s in known_services if s and s in message)
        for svc in candidates:
            mentioned_services[svc] += 1

    top_templates = []
    for template, count in template_counts.most_common(top_n):
        top_templates.append(
            {
                "template": template,
                "count": count,
                "services": [
                    {"service": svc, "count": svc_count}
                    for svc, svc_count in template_services[template].most_common(5)
                ],
                "tokens": tokenize_text(template),
            }
        )

    return {
        "total": len(logs),
        "level_counts": dict(sorted(level_counts.items())),
        "service_counts": dict(sorted(service_counts.items())),
        "service_signal_counts": dict(sorted(service_signal_counts.items())),
        "mentioned_services": dict(sorted(mentioned_services.items())),
        "templates": top_templates,
        "template_counts": dict(template_counts.most_common(top_n)),
        "tokens": weighted_tokens_from_counter(token_counts),
    }


def edge_key(src: str, dst: str) -> str:
    return f"{src}->{dst}"


def trace_anomaly_score(error_rate: float, p99_ratio: float) -> float:
    error_component = min(1.0, error_rate * 2.5)
    latency_component = min(1.0, max(0.0, p99_ratio - 1.0) / 2.5)
    return compact_float((0.6 * error_component) + (0.4 * latency_component))


def extract_trace_features(
    traces: list[dict[str, Any]],
    topology: dict[str, Any] | None = None,
    top_n: int = 20,
) -> dict[str, Any]:
    topo = topology_index(topology)
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in traces:
        src, dst = row.get("from"), row.get("to")
        if src and dst:
            grouped[(src, dst)].append(row)

    edges = []
    token_counts: Counter[str] = Counter()
    service_signal_counts: Counter[str] = Counter()

    for (src, dst), rows in grouped.items():
        rows = sorted(rows, key=lambda row: parse_ts(row.get("ts")))
        if not rows:
            continue
        baseline_len = max(1, len(rows) // 4)
        current_len = max(1, len(rows) // 2)
        baseline = rows[:baseline_len]
        current = rows[-current_len:]

        baseline_p99 = median(float(row.get("p99_ms", 0.0)) for row in baseline)
        current_p99 = median(float(row.get("p99_ms", 0.0)) for row in current)
        p99_ratio = safe_ratio(current_p99, baseline_p99)
        current_count = sum(int(row.get("count", 0)) for row in current)
        current_errors = sum(int(row.get("error_count", 0)) for row in current)
        error_rate = current_errors / current_count if current_count else 0.0
        score = trace_anomaly_score(error_rate, p99_ratio)
        protocol = topo["protocols"].get((src, dst), "unknown")

        edge = {
            "edge": edge_key(src, dst),
            "from": src,
            "to": dst,
            "protocol": protocol,
            "samples": len(rows),
            "count": current_count,
            "error_count": current_errors,
            "error_rate": compact_float(error_rate),
            "p99_baseline_ms": compact_float(baseline_p99),
            "p99_current_ms": compact_float(current_p99),
            "p99_deviation_ratio": compact_float(p99_ratio),
            "score": score,
        }
        edges.append(edge)

        if score >= 0.18 or error_rate >= 0.05 or p99_ratio >= 1.5:
            token_counts[f"edge:{src}->{dst}"] += score or 0.1
            token_counts[f"from:{src}"] += score or 0.1
            token_counts[f"to:{dst}"] += score or 0.1
            token_counts[f"protocol:{protocol}"] += max(score, 0.1)
            service_signal_counts[src] += max(score, 0.1)
            service_signal_counts[dst] += max(score, 0.1)

    edges = sorted(edges, key=lambda edge: edge["score"], reverse=True)
    anomalous_edges = [
        edge
        for edge in edges
        if edge["score"] >= 0.18 or edge["error_rate"] >= 0.05 or edge["p99_deviation_ratio"] >= 1.5
    ]
    return {
        "total": len(traces),
        "edges": edges[:top_n],
        "anomalous_edges": anomalous_edges[:top_n],
        "tokens": weighted_tokens_from_counter(token_counts),
        "service_signal_counts": {
            key: compact_float(value) for key, value in sorted(service_signal_counts.items())
        },
    }


def split_metric_name(name: str) -> tuple[str, str]:
    if "." not in name:
        return "unknown", name
    service, metric = name.split(".", 1)
    return service, metric


def extract_metric_features(metrics_window: dict[str, Any] | None, top_n: int = 20) -> dict[str, Any]:
    samples = (metrics_window or {}).get("samples", {})
    series = []
    token_counts: Counter[str] = Counter()
    service_signal_counts: Counter[str] = Counter()

    for name, rows in samples.items():
        if not rows:
            continue
        values = [float(row[1]) for row in rows if len(row) >= 2]
        if not values:
            continue
        service, metric = split_metric_name(name)
        quarter = max(1, len(values) // 4)
        before = median(values[:quarter])
        after = median(values[-quarter:])
        ratio = safe_ratio(after, before)
        abs_delta = after - before
        score = min(1.0, abs(math.log(max(ratio, 0.0001))) / math.log(5))
        if abs_delta > 0:
            score = max(score, min(1.0, abs_delta / (abs(before) + 1.0)))

        row = {
            "series": name,
            "service": service,
            "metric": metric,
            "baseline": compact_float(before),
            "current": compact_float(after),
            "delta": compact_float(abs_delta),
            "ratio": compact_float(ratio),
            "score": compact_float(score),
        }
        series.append(row)

        if score >= 0.25:
            token_counts[f"metric:{metric}"] += score
            token_counts[f"metric_service:{service}"] += score
            service_signal_counts[service] += score

    series = sorted(series, key=lambda row: row["score"], reverse=True)
    return {
        "total_series": len(samples),
        "series": series[:top_n],
        "tokens": weighted_tokens_from_counter(token_counts),
        "service_signal_counts": {
            key: compact_float(value) for key, value in sorted(service_signal_counts.items())
        },
    }


def derive_affected_services(
    incident: dict[str, Any],
    log_features: dict[str, Any],
    trace_features: dict[str, Any],
    metric_features: dict[str, Any],
) -> list[str]:
    scores: Counter[str] = Counter()
    trigger = (incident.get("trigger_alert") or {}).get("service")
    if trigger:
        scores[trigger] += 1.0

    evidence_services = set(log_features.get("service_counts", {}))
    for edge in trace_features.get("edges", []):
        evidence_services.add(edge.get("from"))
        evidence_services.add(edge.get("to"))
    for series in metric_features.get("series", []):
        evidence_services.add(series.get("service"))
    if trigger:
        evidence_services.add(trigger)
    evidence_services.discard(None)

    for service, count in log_features.get("service_signal_counts", {}).items():
        scores[service] += min(3.0, float(count) / 10.0)
    for service, count in log_features.get("mentioned_services", {}).items():
        if service in evidence_services:
            scores[service] += min(1.0, float(count) / 20.0)
    for service, value in trace_features.get("service_signal_counts", {}).items():
        scores[service] += float(value) * 2.0
    for service, value in metric_features.get("service_signal_counts", {}).items():
        scores[service] += float(value) * 0.75

    selected = [service for service, score in scores.items() if score >= 0.5]
    return sorted(selected, key=lambda service: (-scores[service], service))


def flatten_tokens(*groups: dict[str, float]) -> dict[str, float]:
    merged: Counter[str] = Counter()
    for group in groups:
        for key, value in group.items():
            merged[key] += float(value)
    return weighted_tokens_from_counter(merged)


def extract_features(incident: dict[str, Any]) -> dict[str, Any]:
    """Layer 1 for live/eval incidents."""
    topo = topology_index(incident.get("topology"))
    log_features = extract_log_features(incident.get("logs", []), known_services=topo["services"])
    trace_features = extract_trace_features(incident.get("traces", []), incident.get("topology"))
    metric_features = extract_metric_features(incident.get("metrics_window"))
    affected_services = derive_affected_services(
        incident, log_features, trace_features, metric_features
    )
    trigger_alert = incident.get("trigger_alert") or {}

    service_tokens = {f"service:{svc}": 1.0 for svc in affected_services}
    trigger_service = trigger_alert.get("service")
    if trigger_service:
        service_tokens[f"trigger:{trigger_service}"] = 1.0

    return {
        "incident_id": incident.get("incident_id"),
        "kind": "live",
        "trigger": {
            "service": trigger_service,
            "rule_id": trigger_alert.get("rule_id"),
            "severity": trigger_alert.get("severity"),
        },
        "affected_services": affected_services,
        "log": log_features,
        "trace": trace_features,
        "metric": metric_features,
        "topology": {
            "node_count": len(topo["services"]),
            "affected_tiers": sorted(
                {topo["tiers"].get(service, "unknown") for service in affected_services}
            ),
        },
        "tokens": {
            "log": log_features["tokens"],
            "trace": trace_features["tokens"],
            "metric": metric_features["tokens"],
            "service": service_tokens,
            "all": flatten_tokens(
                log_features["tokens"],
                trace_features["tokens"],
                metric_features["tokens"],
                service_tokens,
            ),
        },
        "evidence_summary": {
            "top_log_templates": log_features["templates"][:5],
            "top_trace_edges": trace_features["anomalous_edges"][:5],
            "top_metric_series": metric_features["series"][:5],
        },
    }


def parse_metric_delta(delta: str) -> tuple[float, float]:
    parts = delta.replace("->", "|").split("|")
    if len(parts) != 2:
        return 0.0, 0.0
    try:
        return float(parts[0].strip()), float(parts[1].strip())
    except ValueError:
        return 0.0, 0.0


def extract_history_features(entry: dict[str, Any]) -> dict[str, Any]:
    """Layer 1 representation for one historical incident entry."""
    log_template_counts: Counter[str] = Counter()
    log_tokens: Counter[str] = Counter()
    for signature in entry.get("log_signatures", []):
        template = normalize_log_message(signature)
        log_template_counts[template] += 1
        log_tokens.update(tokenize_text(template))

    trace_edges = []
    trace_tokens: Counter[str] = Counter()
    trace_service_counts: Counter[str] = Counter()
    for sig in entry.get("trace_signatures", []):
        src, dst = sig.get("from"), sig.get("to")
        if not src or not dst:
            continue
        error_rate = float(sig.get("error_rate", 0.0))
        p99_ratio = float(sig.get("p99_deviation_ratio", 1.0))
        score = trace_anomaly_score(error_rate, p99_ratio)
        edge = {
            "edge": edge_key(src, dst),
            "from": src,
            "to": dst,
            "protocol": "unknown",
            "error_rate": compact_float(error_rate),
            "p99_deviation_ratio": compact_float(p99_ratio),
            "score": score,
        }
        trace_edges.append(edge)
        trace_tokens[f"edge:{src}->{dst}"] += max(score, 0.1)
        trace_tokens[f"from:{src}"] += max(score, 0.1)
        trace_tokens[f"to:{dst}"] += max(score, 0.1)
        trace_service_counts[src] += max(score, 0.1)
        trace_service_counts[dst] += max(score, 0.1)

    metric_series = []
    metric_tokens: Counter[str] = Counter()
    metric_service_counts: Counter[str] = Counter()
    for sig in entry.get("metric_signatures", []):
        before, after = parse_metric_delta(sig.get("delta", ""))
        ratio = safe_ratio(after, before)
        service = sig.get("service", "unknown")
        metric = sig.get("metric", "unknown")
        score = min(1.0, abs(math.log(max(ratio, 0.0001))) / math.log(5))
        metric_series.append(
            {
                "series": f"{service}.{metric}",
                "service": service,
                "metric": metric,
                "baseline": compact_float(before),
                "current": compact_float(after),
                "delta": compact_float(after - before),
                "ratio": compact_float(ratio),
                "score": compact_float(score),
            }
        )
        metric_tokens[f"metric:{metric}"] += max(score, 0.1)
        metric_tokens[f"metric_service:{service}"] += max(score, 0.1)
        metric_service_counts[service] += max(score, 0.1)

    affected_services = sorted(entry.get("affected_services", []))
    service_tokens = {f"service:{svc}": 1.0 for svc in affected_services}
    root_class = entry.get("root_cause_class")

    log_features = {
        "total": sum(log_template_counts.values()),
        "templates": [
            {"template": template, "count": count, "services": [], "tokens": tokenize_text(template)}
            for template, count in log_template_counts.most_common()
        ],
        "template_counts": dict(log_template_counts),
        "tokens": weighted_tokens_from_counter(log_tokens, weight=3.0),
    }
    trace_features = {
        "total": len(trace_edges),
        "edges": sorted(trace_edges, key=lambda row: row["score"], reverse=True),
        "anomalous_edges": sorted(trace_edges, key=lambda row: row["score"], reverse=True),
        "tokens": weighted_tokens_from_counter(trace_tokens),
        "service_signal_counts": {
            key: compact_float(value) for key, value in sorted(trace_service_counts.items())
        },
    }
    metric_features = {
        "total_series": len(metric_series),
        "series": sorted(metric_series, key=lambda row: row["score"], reverse=True),
        "tokens": weighted_tokens_from_counter(metric_tokens),
        "service_signal_counts": {
            key: compact_float(value) for key, value in sorted(metric_service_counts.items())
        },
    }

    return {
        "incident_id": entry.get("id"),
        "kind": "history",
        "root_cause_class": root_class,
        "affected_services": affected_services,
        "outcome": entry.get("outcome"),
        "actions_taken": entry.get("actions_taken", []),
        "mttr_minutes": entry.get("mttr_minutes"),
        "log": log_features,
        "trace": trace_features,
        "metric": metric_features,
        "tokens": {
            "log": log_features["tokens"],
            "trace": trace_features["tokens"],
            "metric": metric_features["tokens"],
            "service": service_tokens,
            "all": flatten_tokens(
                log_features["tokens"],
                trace_features["tokens"],
                metric_features["tokens"],
                service_tokens,
            ),
        },
        "evidence_summary": {
            "top_log_templates": log_features["templates"][:5],
            "top_trace_edges": trace_features["anomalous_edges"][:5],
            "top_metric_series": metric_features["series"][:5],
        },
    }


def extract_history_corpus_features(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [extract_history_features(entry) for entry in history]


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect Layer 1 incident features.")
    parser.add_argument("path", type=Path)
    parser.add_argument("--history", action="store_true", help="Treat input as history corpus.")
    args = parser.parse_args()

    data = json.loads(args.path.read_text(encoding="utf-8"))
    if args.history:
        output = extract_history_corpus_features(data)
    else:
        output = extract_features(data)
    print(json.dumps(output, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

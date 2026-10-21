"""Champion vs challenger: the model in use and a retrained candidate, scored on the same
held-out events, with a markdown report for humans and a promote / keep verdict."""

from __future__ import annotations

from datetime import UTC, datetime

from .dataset import TrainingSet
from .model import RiskModel
from .train import score_metrics

_ROWS = (
    ("AUC-ROC", ("auc_roc",)),
    ("PR-AUC", ("pr_auc",)),
    ("Brier score (lower is better)", ("brier",)),
    ("Recall at score ≥ 40", ("score_ge_40", "recall")),
    ("False-positive rate at score ≥ 40", ("score_ge_40", "false_positive_rate")),
    ("Precision at score ≥ 40", ("score_ge_40", "precision")),
    ("Recall at score > 75", ("score_gt_75", "recall")),
    ("False-positive rate at score > 75", ("score_gt_75", "false_positive_rate")),
)


def compare_models(current: RiskModel | None, candidate: RiskModel, ts: TrainingSet) -> dict:
    _, _, test = ts.split()
    X, y = ts.X[test], ts.y[test]
    report: dict = {
        "created_at": datetime.now(UTC).isoformat(),
        "test_events": int(test.stop - test.start),
        "test_fraud_events": int(y.sum()),
        "label_sources": dict(ts.label_sources),
        "candidate": {"version": candidate.version, **score_metrics(y, candidate.score_matrix(X))},
        "current": None,
        "current_trained_on_test_events": None,
    }
    if current is not None:
        report["current"] = {
            "version": current.version,
            **score_metrics(y, current.score_matrix(X)),
        }
        last = current.metadata.get("training", {}).get("last_train_event_id")
        positions = {eid: i for i, eid in enumerate(ts.event_ids)}
        if last in positions:
            report["current_trained_on_test_events"] = positions[last] >= test.start
    # Promote only on a measured gain: an equally good model is churn, not progress.
    cand_pr = report["candidate"].get("pr_auc", 0.0)
    cur_pr = (report["current"] or {}).get("pr_auc", -1.0)
    report["candidate_wins"] = cand_pr > cur_pr
    return report


def _get(metrics: dict | None, path: tuple[str, ...]) -> float | None:
    value = metrics
    for key in path:
        if not isinstance(value, dict) or key not in value:
            return None
        value = value[key]
    return value


def _fmt(v: float | None) -> str:
    return "—" if v is None else f"{v:.4f}"


def render_markdown(report: dict) -> str:
    cur, cand = report["current"], report["candidate"]
    lines = [
        f"# Model comparison: {cand['version']}",
        "",
        f"Generated {report['created_at']}. Both models scored on the same "
        f"{report['test_events']:,} most recent labelled events "
        f"({report['test_fraud_events']:,} fraudulent), which the candidate never trained on.",
        "",
        "Labels used: "
        + ", ".join(f"{n:,} {source}" for source, n in sorted(report["label_sources"].items()))
        + " (an analyst's verdict takes precedence over ground truth).",
        "",
        f"| Metric | Current ({cur['version'] if cur else 'none'}) | Candidate | Change |",
        "|---|---|---|---|",
    ]
    for name, path in _ROWS:
        a, b = _get(cur, path), _get(cand, path)
        delta = "—" if a is None or b is None else f"{b - a:+.4f}"
        lines.append(f"| {name} | {_fmt(a)} | {_fmt(b)} | {delta} |")
    lines.append("")
    if report["current_trained_on_test_events"]:
        lines += [
            "> **Caution:** the current model was trained on some of these test events, so its",
            "> numbers here are optimistic and the comparison favours it.",
            "",
        ]
    elif cur and report["current_trained_on_test_events"] is None:
        lines += [
            "> Could not check whether the current model trained on these events (its metadata",
            "> predates that check, or it was trained on a different dataset).",
            "",
        ]
    verdict = (
        "**Promote the candidate**: its PR-AUC is higher."
        if report["candidate_wins"]
        else "**Keep the current model**: the candidate's PR-AUC is not higher."
    )
    lines.append(verdict)
    return "\n".join(lines) + "\n"

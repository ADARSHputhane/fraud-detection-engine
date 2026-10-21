<!-- Produced by `fraud-engine retrain --promote-if-better` during the Milestone 6 run. -->

This is the cold-start story: the model in use was the Isolation Forest, trained without a
single label. After analysts worked the review queue, a supervised model trained on their
verdicts (plus ground truth) was compared with it on the same held-out events and promoted.

# Model comparison: xgb-20261021T075849Z

Generated 2026-10-21T07:58:50.864721+00:00. Both models scored on the same 7,085 most recent labelled events (316 fraudulent), which the candidate never trained on.

Labels used: 109 analyst, 35,312 synthetic (an analyst's verdict takes precedence over ground truth).

| Metric | Current (iforest-20261021T070215Z) | Candidate | Change |
|---|---|---|---|
| AUC-ROC | 0.9673 | 0.9842 | +0.0169 |
| PR-AUC | 0.9225 | 0.9409 | +0.0184 |
| Brier score (lower is better) | 0.0270 | 0.0043 | -0.0227 |
| Recall at score ≥ 40 | 0.6329 | 0.9019 | +0.2690 |
| False-positive rate at score ≥ 40 | 0.0000 | 0.0001 | +0.0001 |
| Precision at score ≥ 40 | 1.0000 | 0.9965 | -0.0035 |
| Recall at score > 75 | 0.2722 | 0.8956 | +0.6234 |
| False-positive rate at score > 75 | 0.0000 | 0.0000 | +0.0000 |

> Could not check whether the current model trained on these events (its metadata
> predates that check, or it was trained on a different dataset).

**Promote the candidate**: its PR-AUC is higher.

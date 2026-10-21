// Shapes returned by the FastAPI service (see src/fraud_engine/schemas.py).

export type Decision = "ALLOW" | "REVIEW" | "DENY";

export interface RuleHit {
  id: string;
  name: string;
  action: "DENY" | "REVIEW";
  weight: number;
  reason: string;
}

export interface Contribution {
  feature: string;
  value: unknown;
  contribution: number;
}

export interface QueueItem {
  event_id: string;
  event_type: "payment" | "instrument";
  user_id: string;
  occurred_at: string;
  final_decision: Decision;
  decided_by: string;
  ml_score: number | null;
  rule_score: number;
  amount: string | null;
  currency: string | null;
  merchant_category: string | null;
  top_rule: RuleHit | null;
  top_feature: Contribution | null;
  analyst_label: boolean | null;
}

export interface DecisionPage {
  total: number;
  items: QueueItem[];
}

export interface StoredDecision {
  event_id: string;
  event_type: string;
  user_id: string;
  occurred_at: string;
  decided_at: string;
  final_decision: Decision;
  decided_by: string;
  rules_fired: RuleHit[];
  rule_score: number;
  ruleset_version: string;
  ml_score: number | null;
  model_version: string | null;
  shap_top5: Contribution[] | null;
  thresholds: { review_at: number; deny_above: number };
  latency_ms: number;
  stage_ms: Record<string, number>;
}

export interface FeatureValue {
  name: string;
  group: string;
  kind: "bool" | "count" | "float" | "str";
  description: string;
  value: unknown;
}

export interface HistoryItem {
  event_id: string;
  event_type: string;
  occurred_at: string;
  final_decision: Decision;
  ml_score: number | null;
  amount: string | null;
  merchant_category: string | null;
  country: string | null;
}

export interface Label {
  event_id: string;
  is_fraud: boolean;
  source: string;
  analyst: string | null;
  note: string | null;
  labelled_at: string;
}

export interface Case {
  event: Record<string, unknown>;
  decision: StoredDecision;
  features: FeatureValue[];
  label: Label | null;
  history: HistoryItem[];
}

export interface Percentiles {
  p50?: number;
  p95?: number;
  p99?: number;
  max?: number;
}

export interface Stats {
  decisions: { total: number; ALLOW: number; REVIEW: number; DENY: number };
  decided_by: { rule: number; model: number; default: number };
  review_queue: number;
  analyst: {
    labelled: number;
    confirmed_fraud: number;
    confirmed_rate: Record<Decision, number | null>;
    agreement_with_ground_truth: number | null;
  };
  ground_truth: {
    labelled_events: number;
    fraud_events: number;
    recall: number | null;
    false_positive_rate: number | null;
    precision: number | null;
  } | null;
  rule_hits: { id: string; name: string; hits: number }[];
  score_histogram: { from: number; to: number; count: number }[];
  by_day: { day: string; ALLOW: number; REVIEW: number; DENY: number }[];
  latency_ms: { processing: Percentiles; end_to_end: Percentiles };
  model: { version: string; kind: string; auc_roc: number | null; pr_auc: number | null } | null;
}

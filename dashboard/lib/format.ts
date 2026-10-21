// Display formatting. Times are shown in UTC, labelled as such, so every analyst reads the
// same clock as the audit log.

const dateTime = new Intl.DateTimeFormat("en-GB", {
  day: "2-digit",
  month: "short",
  hour: "2-digit",
  minute: "2-digit",
  timeZone: "UTC",
});

export function when(iso: string): string {
  return `${dateTime.format(new Date(iso))} UTC`;
}

export function money(amount: string | null | undefined, currency?: string | null): string {
  if (amount == null) return "—";
  const n = Number(amount);
  try {
    return new Intl.NumberFormat("en-IN", {
      style: "currency",
      currency: currency ?? "INR",
      maximumFractionDigits: 2,
    }).format(n);
  } catch {
    return `${n.toFixed(2)} ${currency ?? ""}`.trim();
  }
}

export function count(n: number | null | undefined): string {
  if (n == null) return "—";
  return new Intl.NumberFormat("en-US", {
    notation: n >= 10_000 ? "compact" : "standard",
    maximumFractionDigits: 1,
  }).format(n);
}

export function pct(rate: number | null | undefined, digits = 1): string {
  return rate == null ? "—" : `${(rate * 100).toFixed(digits)}%`;
}

export function ms(v: number | null | undefined): string {
  if (v == null) return "—";
  if (v >= 1000) return `${(v / 1000).toFixed(1)} s`;
  return `${v < 10 ? v.toFixed(1) : Math.round(v)} ms`;
}

export function humanize(name: string | null | undefined): string {
  if (!name) return "—";
  const s = name.replace(/_/g, " ");
  return s.charAt(0).toUpperCase() + s.slice(1);
}

export function featureValue(value: unknown, name = ""): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (typeof value === "number") {
    if (name.endsWith("_hours")) {
      return value >= 48 ? `${(value / 24).toFixed(1)} days` : `${value.toFixed(1)} h`;
    }
    if (name.endsWith("_km")) return `${Math.round(value).toLocaleString("en-US")} km`;
    if (name.endsWith("_kmh")) return `${Math.round(value).toLocaleString("en-US")} km/h`;
    if (name === "hour_share") return pct(value, 0);
    return Number.isInteger(value) ? value.toLocaleString("en-US") : value.toFixed(2);
  }
  return String(value);
}

export function band(
  score: number,
  thresholds = { review_at: 40, deny_above: 75 },
): "ALLOW" | "REVIEW" | "DENY" {
  if (score > thresholds.deny_above) return "DENY";
  if (score >= thresholds.review_at) return "REVIEW";
  return "ALLOW";
}

// Short, plain-English names for the engine's signals (descriptions live in the API).
const FEATURE_LABELS: Record<string, string> = {
  event_type: "Event type",
  merchant_category: "Merchant category",
  is_payment: "Is a payment",
  amount: "Amount",
  payment_approved: "Payment approved",
  channel_card_present: "Card present",
  high_risk_category: "High-risk category",
  cards_added_1h: "Cards added, 1 h",
  cards_added_3h: "Cards added, 3 h",
  cards_added_24h: "Cards added, 24 h",
  card_txn_count_5m: "Card payments, 5 min",
  card_txn_count_1h: "Card payments, 1 h",
  device_txn_count_5m: "Device payments, 5 min",
  device_txn_count_1h: "Device payments, 1 h",
  device_distinct_cards_24h: "Cards on this device, 24 h",
  ip_distinct_cards_24h: "Cards on this IP, 24 h",
  card_failed_attempts_1h: "Declines on card, 1 h",
  user_total_amount_1h: "User's spend, 1 h",
  user_payments_90d: "Payments in baseline",
  amount_zscore: "Amount z-score",
  amount_vs_typical: "Amount vs. typical",
  hour_share: "Usual at this hour",
  unusual_hour: "Unusual hour",
  category_first_time: "New category for user",
  account_age_hours: "Account age",
  device_age_hours: "Device age",
  new_device: "New device",
  distance_from_last_km: "Distance from last payment",
  travel_speed_kmh: "Travel speed",
  impossible_travel: "Impossible travel",
  new_country: "New country",
  geo_bin_country_mismatch: "Outside card's issuing country",
  datacenter_ip: "Datacenter IP",
  card_age_hours: "Card age",
  bin_risk: "BIN risk",
  card_name_mismatch: "Cardholder name mismatch",
  new_card_high_amount: "New card, high amount",
};

export function featureLabel(name: string): string {
  return FEATURE_LABELS[name] ?? humanize(name);
}

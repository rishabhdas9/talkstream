import { resolveApiBaseUrl } from "./endpoints.ts";
import { errorForResponse } from "./errors.ts";

export type OperationsComponent = {
  id: string;
  provider: string;
  model: string | null;
  configured: boolean;
};

export type ObservedProvider = {
  provider: string;
  model: string;
  attempts: number;
  succeeded: number;
  failed: number;
  cancelled: number;
  unknown: number;
  last_outcome: string | null;
  last_attempt_at: string;
  last_success_at: string | null;
};

export type PeriodSpend = {
  settled_micro_inr: number;
  held_micro_inr: number;
  limit_micro_inr: number;
};

export type OperationsStatus = {
  runtime_profile: string;
  components: OperationsComponent[];
  observed: ObservedProvider[];
  rates: {
    configured: boolean;
    version: string | null;
    qualified: boolean | null;
    valid_until: string | null;
  };
  paid_work_enabled: boolean;
  active_calls: number;
  spend: {
    provider_today: PeriodSpend;
    provider_month: PeriodSpend;
    disputed_attempts: number;
    wallet_today_paise: number;
  };
};

export type OperationsPolicy = {
  content_policy_version: string;
  runtime_policy_version: string;
  runtime_profile: string;
  enforcement_points: { id: string; description: string }[];
  classifier_timeout_seconds: number;
  max_text_bytes: number;
  examples: { language: string; allowed: number; blocked: number }[];
  limits: Record<string, number>;
};

export type ProviderAttempt = {
  id: string;
  created_at: string;
  provider: string;
  model: string;
  state: string;
  outcome: string | null;
  scope: "call" | "job";
  maximum_micro_inr: number;
  actual_micro_inr: number | null;
};

export type PolicyEventSummary = {
  occurred_at: string;
  stage: string;
  category: string;
  allowed: boolean;
  duration_ms: number;
  policy_version: string;
};

export type OperationsEvents = {
  attempts: ProviderAttempt[];
  next_before: string | null;
  policy_counts: { stage: string; category: string; count: number }[];
  policy_recent: PolicyEventSummary[];
};

export type ComponentHealth = { tone: "idle" | "ok" | "warn" | "down"; label: string };

// A configured key is not vendor health: only an observed request outcome is.
export function componentHealth(
  component: OperationsComponent,
  observed: ObservedProvider[],
): ComponentHealth {
  if (!component.configured) return { tone: "idle", label: "Not configured" };
  if (component.model === null) return { tone: "idle", label: "Configured · outcomes not tracked" };
  const row = observed.find(
    (item) => item.provider === component.provider && item.model === component.model,
  );
  if (!row) return { tone: "idle", label: "Configured · no requests in 24 h" };
  if (row.last_outcome === "succeeded") return { tone: "ok", label: "Last request succeeded" };
  if (row.last_outcome === null) return { tone: "idle", label: "Request in progress" };
  if (row.last_outcome === "unknown") return { tone: "warn", label: "Last outcome unknown" };
  return { tone: "down", label: `Last request ${row.last_outcome}` };
}

export function formatMicroInr(value: number | null): string {
  return value == null ? "unknown" : `₹${(value / 1_000_000).toFixed(4)}`;
}

async function getJson<T>(path: string, token: string): Promise<T> {
  // Resolved per call so this module stays importable outside the browser.
  const base = resolveApiBaseUrl(import.meta.env.VITE_API_BASE_URL, window.location.origin);
  const response = await fetch(`${base}${path}`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!response.ok) throw await errorForResponse(response);
  return (await response.json()) as T;
}

export const getOperationsStatus = (token: string) =>
  getJson<OperationsStatus>("/operations/status", token);

export const getOperationsPolicy = (token: string) =>
  getJson<OperationsPolicy>("/operations/policy", token);

export const getOperationsEvents = (token: string, before: string | null = null) =>
  getJson<OperationsEvents>(
    `/operations/events${before ? `?before=${encodeURIComponent(before)}` : ""}`,
    token,
  );

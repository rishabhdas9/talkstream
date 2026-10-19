import { resolveApiBaseUrl } from "./endpoints";
import { errorForResponse } from "./errors";

const API_BASE_URL = resolveApiBaseUrl(
  import.meta.env.VITE_API_BASE_URL,
  window.location.origin,
);

export type AnalyticsWindow = "hour" | "day" | "week";

export type AnalyticsTotals = {
  sessions: number;
  billed_seconds: number;
  cost_paise: number;
  avg_session_seconds: number | null;
  interrupted_turns: number;
  total_turns: number;
};

export type AnalyticsBucket = {
  start: string;
  billed_seconds: number;
  cost_paise: number;
  sessions: number;
};

export type SessionSummary = {
  id: string;
  started_at: string;
  ended_at: string | null;
  billed_seconds: number;
  cost_paise: number;
  end_reason: string | null;
  languages: string[];
  turns: number;
  interrupted_turns: number;
};

export type StagePercentiles = {
  key: string;
  count: number;
  p50: number | null;
  p95: number | null;
  p99: number | null;
};

export type LatencyRollup = {
  stages: StagePercentiles[];
  valid_turns: number;
  excluded_turns: number;
};

export type AnalyticsResponse = {
  window: AnalyticsWindow;
  window_start: string;
  bucket_seconds: number;
  totals: AnalyticsTotals;
  buckets: AnalyticsBucket[];
  sessions: SessionSummary[];
  latency: LatencyRollup;
};

export type SessionTurn = {
  turn_index: number;
  created_at: string;
  language_code: string | null;
  interrupted: boolean;
  tool_names: string[];
  e2e_voice_to_voice_ms: number | null;
  stages: Record<string, number | null>;
  barge_in_stop_ack_ms: number | null;
};

export type SessionDetailResponse = {
  session: SessionSummary;
  turns: SessionTurn[];
};

async function getJson<T>(path: string, token: string): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!response.ok) {
    throw await errorForResponse(response);
  }
  return (await response.json()) as T;
}

export async function getAnalytics(
  token: string,
  window: AnalyticsWindow,
): Promise<AnalyticsResponse> {
  return getJson<AnalyticsResponse>(`/analytics?window=${window}`, token);
}

export async function getSessionDetail(
  token: string,
  sessionId: string,
): Promise<SessionDetailResponse> {
  return getJson<SessionDetailResponse>(
    `/analytics/sessions/${encodeURIComponent(sessionId)}`,
    token,
  );
}

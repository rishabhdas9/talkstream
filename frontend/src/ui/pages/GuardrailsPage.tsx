import { useEffect, useState } from "react";

import { isAuthenticationExpiredError } from "../../api/errors";
import {
  formatMicroInr,
  getOperationsEvents,
  getOperationsPolicy,
  type OperationsEvents,
  type OperationsPolicy,
} from "../../api/operations";
import { formatDateTime } from "../format";

type GuardrailsPageProps = {
  token: string;
  onAuthenticationExpired: () => void;
};

const CARD = {
  background: "#FFFFFF",
  border: "1px solid #E5E5E1",
  borderRadius: 14,
  padding: "18px 20px",
} as const;

const CELL = {
  padding: "9px 12px",
  fontSize: 12.5,
  borderBottom: "1px solid #F4F4F0",
  textAlign: "left" as const,
};

const HEAD = {
  ...CELL,
  fontSize: 11,
  fontWeight: 500,
  color: "#6B6B66",
  textTransform: "uppercase" as const,
  letterSpacing: "0.07em",
};

function formatLimit(key: string, value: number): string {
  return key.endsWith("_micro_inr") ? formatMicroInr(value) : String(value);
}

export function GuardrailsPage({ token, onAuthenticationExpired }: GuardrailsPageProps) {
  const [policy, setPolicy] = useState<OperationsPolicy | null>(null);
  const [events, setEvents] = useState<OperationsEvents | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;
    Promise.all([getOperationsPolicy(token), getOperationsEvents(token)])
      .then(([loadedPolicy, loadedEvents]) => {
        if (cancelled) return;
        setPolicy(loadedPolicy);
        setEvents(loadedEvents);
      })
      .catch((reason: unknown) => {
        if (cancelled) return;
        if (isAuthenticationExpiredError(reason)) onAuthenticationExpired();
        else setError(reason instanceof Error ? reason.message : String(reason));
      });
    return () => {
      cancelled = true;
    };
  }, [token, onAuthenticationExpired]);

  return (
    <div className="content-page guardrails-page" style={{ padding: "var(--page-gutter)", maxWidth: 1020, margin: "0 auto" }}>
      <h1 style={{ margin: 0, fontSize: 26, fontWeight: 500, letterSpacing: "-0.02em" }}>
        Guardrails
      </h1>
      <div style={{ fontSize: 13, color: "#6B6B66", marginTop: 6 }}>
        Effective policy and recorded check outcomes. Policy is versioned deployment
        configuration and cannot be edited here.
      </div>

      {error ? (
        <div style={{ ...CARD, marginTop: 22, color: "#B3352E", fontSize: 13 }}>{error}</div>
      ) : !policy || !events ? (
        <div style={{ marginTop: 22, fontSize: 13, color: "#6B6B66" }}>Loading…</div>
      ) : (
        <>
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fit,minmax(min(100%,260px),1fr))",
              gap: 16,
              marginTop: 22,
              alignItems: "start",
            }}
          >
            <div style={CARD}>
              <div style={{ fontSize: 13.5, fontWeight: 600 }}>
                Content policy {policy.content_policy_version} · runtime policy{" "}
                {policy.runtime_policy_version} ({policy.runtime_profile})
              </div>
              <div style={{ fontSize: 12.5, color: "#6B6B66", marginTop: 6 }}>
                Classifier timeout {policy.classifier_timeout_seconds} s · inspected text up to{" "}
                {policy.max_text_bytes} bytes. A failed required check blocks the answer or action.
              </div>
              {policy.enforcement_points.map((point) => (
                <div
                  key={point.id}
                  style={{ padding: "10px 0", borderBottom: "1px solid #F4F4F0", fontSize: 13 }}
                >
                  <span style={{ fontWeight: 600 }}>{point.id.replace("_", " ")}</span>
                  <span style={{ color: "#6B6B66" }}> — {point.description}</span>
                </div>
              ))}
            </div>
            <div style={CARD}>
              <div style={{ fontSize: 13.5, fontWeight: 600 }}>Limits</div>
              {Object.entries(policy.limits).map(([key, value]) => (
                <div
                  key={key}
                  style={{
                    display: "flex",
                    justifyContent: "space-between",
                    gap: 12,
                    padding: "7px 0",
                    borderBottom: "1px solid #F4F4F0",
                    fontSize: 12.5,
                  }}
                >
                  <span style={{ color: "#6B6B66" }}>
                    {key.replace(/_micro_inr$/, " budget").replaceAll("_", " ")}
                  </span>
                  <span style={{ fontVariantNumeric: "tabular-nums" }}>
                    {formatLimit(key, value)}
                  </span>
                </div>
              ))}
              <div style={{ fontSize: 13.5, fontWeight: 600, marginTop: 16 }}>
                Policy examples (allowed / blocked)
              </div>
              <div style={{ fontSize: 12.5, color: "#6B6B66", marginTop: 6 }}>
                {policy.examples
                  .map((item) => `${item.language} ${item.allowed}/${item.blocked}`)
                  .join(" · ")}
              </div>
            </div>
          </div>

          <div style={{ ...CARD, padding: 0, marginTop: 16, overflowX: "auto" }}>
            <div style={{ padding: "18px 20px 8px", fontSize: 13.5, fontWeight: 600 }}>
              Check outcomes
            </div>
            <div style={{ padding: "0 20px 10px", fontSize: 12, color: "#6B6B66" }}>
              Metadata only: no transcripts or answers are stored. No false-positive review labels
              have been recorded yet.
            </div>
            {events.policy_counts.length === 0 ? (
              <div style={{ padding: "4px 20px 18px", fontSize: 12.5, color: "#6B6B66" }}>
                No policy checks recorded.
              </div>
            ) : (
              <table style={{ width: "100%", borderCollapse: "collapse" }}>
                <thead>
                  <tr>
                    <th style={HEAD}>Stage</th>
                    <th style={HEAD}>Category</th>
                    <th style={HEAD}>Count</th>
                  </tr>
                </thead>
                <tbody>
                  {events.policy_counts.map((row) => (
                    <tr key={`${row.stage}:${row.category}`}>
                      <td style={CELL}>{row.stage}</td>
                      <td style={CELL}>{row.category}</td>
                      <td style={CELL}>{row.count}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>

          {events.policy_recent.length > 0 && (
            <div style={{ ...CARD, padding: 0, marginTop: 16, overflowX: "auto" }}>
              <div style={{ padding: "18px 20px 8px", fontSize: 13.5, fontWeight: 600 }}>
                Recent checks
              </div>
              <table style={{ width: "100%", borderCollapse: "collapse" }}>
                <thead>
                  <tr>
                    {["Time", "Stage", "Category", "Result", "Duration"].map((label) => (
                      <th key={label} style={HEAD}>
                        {label}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {events.policy_recent.map((event, index) => (
                    <tr key={`${event.occurred_at}:${index}`}>
                      <td style={CELL}>{formatDateTime(event.occurred_at)}</td>
                      <td style={CELL}>{event.stage}</td>
                      <td style={CELL}>{event.category}</td>
                      <td style={CELL}>{event.allowed ? "Allowed" : "Blocked"}</td>
                      <td style={CELL}>{Math.round(event.duration_ms)} ms</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
    </div>
  );
}

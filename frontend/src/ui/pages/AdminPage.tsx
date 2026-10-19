import { useEffect, useState } from "react";

import { isAuthenticationExpiredError } from "../../api/errors";
import {
  componentHealth,
  formatMicroInr,
  getOperationsEvents,
  getOperationsStatus,
  type OperationsEvents,
  type OperationsStatus,
  type PeriodSpend,
  type ProviderAttempt,
} from "../../api/operations";
import { formatDateTime, formatRupees } from "../format";

type AdminPageProps = {
  token: string;
  onAuthenticationExpired: () => void;
};

const COMPONENT_NAMES: Record<string, string> = {
  speech_recognition: "Speech recognition",
  text: "Text model",
  speech_synthesis: "Speech synthesis",
  web_search: "Web search",
  email: "Email delivery",
  knowledge: "Knowledge store",
};

const TONE_COLOR = { idle: "#A6A6A0", ok: "#22A45D", warn: "#D97706", down: "#B3352E" };

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

function SpendRow({ label, spend }: { label: string; spend: PeriodSpend }) {
  return (
    <div style={{ fontSize: 13, marginTop: 8 }}>
      <span style={{ color: "#6B6B66" }}>{label}: </span>
      settled {formatMicroInr(spend.settled_micro_inr)} · held{" "}
      {formatMicroInr(spend.held_micro_inr)} of {formatMicroInr(spend.limit_micro_inr)}
    </div>
  );
}

export function AdminPage({ token, onAuthenticationExpired }: AdminPageProps) {
  const [status, setStatus] = useState<OperationsStatus | null>(null);
  const [attempts, setAttempts] = useState<ProviderAttempt[]>([]);
  const [nextBefore, setNextBefore] = useState<string | null>(null);
  const [error, setError] = useState("");

  function fail(reason: unknown) {
    if (isAuthenticationExpiredError(reason)) onAuthenticationExpired();
    else setError(reason instanceof Error ? reason.message : String(reason));
  }

  function showEvents(events: OperationsEvents, append: boolean) {
    setAttempts((current) => (append ? [...current, ...events.attempts] : events.attempts));
    setNextBefore(events.next_before);
  }

  useEffect(() => {
    let cancelled = false;
    Promise.all([getOperationsStatus(token), getOperationsEvents(token)])
      .then(([loaded, events]) => {
        if (cancelled) return;
        setStatus(loaded);
        showEvents(events, false);
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
    <div className="content-page admin-page" style={{ padding: "var(--page-gutter)", maxWidth: 1120, margin: "0 auto" }}>
      <h1 style={{ margin: 0, fontSize: 26, fontWeight: 500, letterSpacing: "-0.02em" }}>
        Operations
      </h1>
      <div style={{ fontSize: 13, color: "#6B6B66", marginTop: 6 }}>
        Configured state and observed provider requests. A configured key is not vendor health.
      </div>

      {error ? (
        <div style={{ ...CARD, marginTop: 22, color: "#B3352E", fontSize: 13 }}>{error}</div>
      ) : !status ? (
        <div style={{ marginTop: 22, fontSize: 13, color: "#6B6B66" }}>Loading…</div>
      ) : (
        <>
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fit,minmax(170px,1fr))",
              gap: 12,
              marginTop: 22,
            }}
          >
            {status.components.map((component) => {
              const health = componentHealth(component, status.observed);
              return (
                <div key={component.id} style={{ ...CARD, padding: 16 }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 7 }}>
                    <span
                      style={{
                        width: 8,
                        height: 8,
                        borderRadius: "50%",
                        background: TONE_COLOR[health.tone],
                        flexShrink: 0,
                      }}
                    />
                    <span style={{ fontSize: 12.5, fontWeight: 600 }}>
                      {COMPONENT_NAMES[component.id] ?? component.id}
                    </span>
                  </div>
                  <div style={{ fontSize: 11.5, color: "#6B6B66", marginTop: 6 }}>
                    {component.provider}
                    {component.model ? ` · ${component.model}` : ""}
                  </div>
                  <div style={{ fontSize: 12, marginTop: 8 }}>{health.label}</div>
                </div>
              );
            })}
          </div>

          <div
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fit,minmax(min(100%,260px),1fr))",
              gap: 14,
              marginTop: 14,
            }}
          >
            <div style={CARD}>
              <div style={{ fontSize: 13.5, fontWeight: 600 }}>Provider spending (estimated)</div>
              <SpendRow label="Today" spend={status.spend.provider_today} />
              <SpendRow label="This month" spend={status.spend.provider_month} />
              <div style={{ fontSize: 12, color: "#6B6B66", marginTop: 10 }}>
                Held amounts are reservations and uncertain outcomes, never assumed free.
                {status.spend.disputed_attempts > 0 &&
                  ` ${status.spend.disputed_attempts} disputed attempt(s) block paid work until reconciled.`}
              </div>
            </div>
            <div style={CARD}>
              <div style={{ fontSize: 13.5, fontWeight: 600 }}>Runtime</div>
              <div style={{ fontSize: 13, marginTop: 8 }}>
                Profile {status.runtime_profile} · active calls {status.active_calls}
              </div>
              <div style={{ fontSize: 13, marginTop: 8 }}>
                Rate card:{" "}
                {status.rates.configured
                  ? `${status.rates.version} (${status.rates.qualified ? "qualified" : "unqualified"})`
                  : "not configured"}{" "}
                · paid calls {status.paid_work_enabled ? "enabled" : "disabled"}
              </div>
              <div style={{ fontSize: 13, marginTop: 8 }}>
                Mock wallet charges today: {formatRupees(status.spend.wallet_today_paise)}
                <span style={{ color: "#6B6B66" }}> (customer credit, not provider cost)</span>
              </div>
            </div>
          </div>

          <div style={{ ...CARD, padding: 0, marginTop: 14, overflowX: "auto" }}>
            <div style={{ padding: "18px 20px 8px", fontSize: 13.5, fontWeight: 600 }}>
              Observed provider requests (24 h)
            </div>
            {status.observed.length === 0 ? (
              <div style={{ padding: "4px 20px 18px", fontSize: 12.5, color: "#6B6B66" }}>
                No provider requests recorded.
              </div>
            ) : (
              <table style={{ width: "100%", borderCollapse: "collapse" }}>
                <thead>
                  <tr>
                    {["Provider", "Model", "Attempts", "Succeeded", "Failed", "Cancelled", "Unknown", "Last outcome", "Last success"].map(
                      (label) => (
                        <th key={label} style={HEAD}>
                          {label}
                        </th>
                      ),
                    )}
                  </tr>
                </thead>
                <tbody>
                  {status.observed.map((row) => (
                    <tr key={`${row.provider}:${row.model}`}>
                      <td style={CELL}>{row.provider}</td>
                      <td style={CELL}>{row.model}</td>
                      <td style={CELL}>{row.attempts}</td>
                      <td style={CELL}>{row.succeeded}</td>
                      <td style={CELL}>{row.failed}</td>
                      <td style={CELL}>{row.cancelled}</td>
                      <td style={CELL}>{row.unknown}</td>
                      <td style={CELL}>{row.last_outcome ?? "in progress"}</td>
                      <td style={CELL}>
                        {row.last_success_at ? formatDateTime(row.last_success_at) : "none"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>

          <div style={{ ...CARD, padding: 0, marginTop: 14, overflowX: "auto" }}>
            <div style={{ padding: "18px 20px 8px", fontSize: 13.5, fontWeight: 600 }}>
              Recent provider attempts
            </div>
            {attempts.length === 0 ? (
              <div style={{ padding: "4px 20px 18px", fontSize: 12.5, color: "#6B6B66" }}>
                No attempts recorded.
              </div>
            ) : (
              <table style={{ width: "100%", borderCollapse: "collapse" }}>
                <thead>
                  <tr>
                    {["Time", "Provider", "Scope", "State", "Outcome", "Reserved max", "Settled"].map(
                      (label) => (
                        <th key={label} style={HEAD}>
                          {label}
                        </th>
                      ),
                    )}
                  </tr>
                </thead>
                <tbody>
                  {attempts.map((attempt) => (
                    <tr key={attempt.id}>
                      <td style={CELL}>{formatDateTime(attempt.created_at)}</td>
                      <td style={CELL}>
                        {attempt.provider} · {attempt.model}
                      </td>
                      <td style={CELL}>{attempt.scope}</td>
                      <td style={CELL}>{attempt.state}</td>
                      <td style={CELL}>{attempt.outcome ?? "in progress"}</td>
                      <td style={CELL}>{formatMicroInr(attempt.maximum_micro_inr)}</td>
                      <td style={CELL}>{formatMicroInr(attempt.actual_micro_inr)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            {nextBefore && (
              <button
                type="button"
                onClick={() =>
                  getOperationsEvents(token, nextBefore)
                    .then((events) => showEvents(events, true))
                    .catch(fail)
                }
                style={{
                  margin: "12px 20px 16px",
                  border: "1px solid #E5E5E1",
                  background: "#FFFFFF",
                  borderRadius: 999,
                  padding: "6px 14px",
                  fontSize: 12.5,
                }}
              >
                Load older attempts
              </button>
            )}
          </div>
        </>
      )}
    </div>
  );
}

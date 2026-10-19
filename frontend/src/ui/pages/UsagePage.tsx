import { useEffect, useState } from "react";

import {
  getAnalytics,
  getSessionDetail,
  type AnalyticsResponse,
  type AnalyticsWindow,
  type SessionDetailResponse,
} from "../../api/analytics";
import { isAuthenticationExpiredError } from "../../api/errors";
import {
  formatDateTime,
  formatLongDuration,
  formatRupees,
  formatTime,
} from "../format";
import { LATENCY_STAGES } from "../latency";

type UsagePageProps = {
  token: string;
  openSessionId: string | null;
  onSessionOpened: () => void;
  pushToast: (message: string) => void;
  onAuthenticationExpired: () => void;
};

const REASON_STYLE: Record<string, [string, string, string]> = {
  user: ["Completed", "#F1F1ED", "#5C5C57"],
  balance_exhausted: ["Balance exhausted", "#FCF3E0", "#9A6A0B"],
  error: ["Error", "#FBEAEA", "#B3352E"],
  live: ["Live", "#E8F3EC", "#1F7A46"],
};

function reasonPill(reason: string | null) {
  const [label, bg, fg] = REASON_STYLE[reason ?? "live"] ?? REASON_STYLE.error;
  return (
    <span
      style={{
        fontSize: 11.5,
        fontWeight: 500,
        borderRadius: 999,
        padding: "3px 10px",
        background: bg,
        color: fg,
      }}
    >
      {label}
    </span>
  );
}

const HEADER_CELL = {
  textAlign: "left" as const,
  fontSize: 11,
  fontWeight: 500,
  letterSpacing: "0.07em",
  textTransform: "uppercase" as const,
  color: "#6B6B66",
  padding: "8px 12px",
  borderBottom: "1px solid #E5E5E1",
};

function bucketLabel(iso: string, window: AnalyticsWindow): string {
  if (window === "week") {
    return new Date(iso).toLocaleDateString("en-IN", { month: "short", day: "numeric" });
  }
  return formatTime(iso);
}

export function UsagePage({
  token,
  openSessionId,
  onSessionOpened,
  pushToast,
  onAuthenticationExpired,
}: UsagePageProps) {
  const [window_, setWindow] = useState<AnalyticsWindow>("day");
  const [data, setData] = useState<AnalyticsResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [detail, setDetail] = useState<SessionDetailResponse | null>(null);
  const [barTip, setBarTip] = useState(-1);
  const [lineTip, setLineTip] = useState(-1);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    getAnalytics(token, window_)
      .then((response) => {
        if (!cancelled) setData(response);
      })
      .catch((error: unknown) => {
        if (isAuthenticationExpiredError(error)) onAuthenticationExpired();
        else pushToast(String(error));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [token, window_, pushToast, onAuthenticationExpired]);

  useEffect(() => {
    if (!openSessionId) return;
    getSessionDetail(token, openSessionId)
      .then(setDetail)
      .catch((error: unknown) => {
        if (isAuthenticationExpiredError(error)) onAuthenticationExpired();
        else pushToast(String(error));
      })
      .finally(onSessionOpened);
  }, [token, openSessionId, onSessionOpened, pushToast, onAuthenticationExpired]);

  const seg = (value: AnalyticsWindow, label: string) => (
    <button
      key={value}
      type="button"
      onClick={() => setWindow(value)}
      style={{
        border: "none",
        borderRadius: 999,
        padding: "6px 14px",
        fontSize: 12.5,
        fontWeight: 500,
        background: window_ === value ? "#FFFFFF" : "transparent",
        color: window_ === value ? "#111110" : "#6B6B66",
      }}
    >
      {label}
    </button>
  );

  if (loading && !data) {
    return (
      <div className="content-page usage-page" style={{ padding: "var(--page-gutter)", maxWidth: 1040, margin: "0 auto" }}>
        <div
          style={{
            height: 30,
            width: 220,
            borderRadius: 8,
            background: "linear-gradient(90deg,#EFEFEB 25%,#F2F2EE 37%,#EFEFEB 63%)",
            backgroundSize: "420px 100%",
            animation: "shimmer 1.3s linear infinite",
          }}
        />
        <div
          style={{ display: "grid", gridTemplateColumns: "var(--usage-summary-columns)", gap: 16, marginTop: 28 }}
        >
          {[0, 1, 2, 3].map((index) => (
            <div
              key={index}
              style={{
                height: 110,
                borderRadius: 14,
                background: "linear-gradient(90deg,#EFEFEB 25%,#F2F2EE 37%,#EFEFEB 63%)",
                backgroundSize: "420px 100%",
                animation: "shimmer 1.3s linear infinite",
              }}
            />
          ))}
        </div>
        <div
          style={{
            height: 320,
            borderRadius: 14,
            marginTop: 16,
            background: "linear-gradient(90deg,#EFEFEB 25%,#F2F2EE 37%,#EFEFEB 63%)",
            backgroundSize: "420px 100%",
            animation: "shimmer 1.3s linear infinite",
          }}
        />
      </div>
    );
  }

  if (!data) return null;

  const subLabel =
    window_ === "hour" ? "last 60 min" : window_ === "day" ? "last 24 h" : "last 7 days";
  const totalMinutes = data.totals.billed_seconds / 60;
  const cards: Array<[string, string]> = [
    ["Minutes used", `${totalMinutes.toFixed(1)} min`],
    ["Wallet charges (mock)", formatRupees(data.totals.cost_paise)],
    ["Calls", String(data.totals.sessions)],
    [
      "Avg call length",
      data.totals.avg_session_seconds == null
        ? "—"
        : formatLongDuration(Math.round(data.totals.avg_session_seconds)),
    ],
  ];

  const buckets = data.buckets;
  const maxMinutes = Math.max(...buckets.map((bucket) => bucket.billed_seconds / 60), 1);
  const costs = buckets.map((bucket) => bucket.cost_paise / 100);
  const maxCost = Math.max(...costs, 1);
  const count = Math.max(costs.length, 2);
  const linePoints = costs
    .map((value, index) => {
      const x = 6 + (index * 508) / (count - 1);
      const y = 142 - (value / maxCost) * 112;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");

  const eligibleStages = data.latency.stages.filter((stage) => stage.count > 0);

  return (
    <div className="content-page usage-page" style={{ padding: "var(--page-gutter)", maxWidth: 1040, margin: "0 auto" }}>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          gap: 16,
          flexWrap: "wrap",
        }}
      >
        <h1 style={{ margin: 0, fontSize: 26, fontWeight: 500, letterSpacing: "-0.02em" }}>
          Usage
        </h1>
        <div
          style={{
            display: "inline-flex",
            background: "#EFEFEB",
            borderRadius: 999,
            padding: 3,
            gap: 2,
          }}
        >
          {seg("hour", "Last 60 min")}
          {seg("day", "24 h")}
          {seg("week", "7 days")}
        </div>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "var(--usage-summary-columns)", gap: 14, marginTop: 22 }}>
        {cards.map(([label, value]) => (
          <div
            key={label}
            style={{
              background: "#FFFFFF",
              border: "1px solid #E5E5E1",
              borderRadius: 14,
              padding: 18,
            }}
          >
            <div
              style={{
                fontSize: 11,
                fontWeight: 600,
                letterSpacing: "0.08em",
                textTransform: "uppercase",
                color: "#6B6B66",
              }}
            >
              {label}
            </div>
            <div
              style={{
                fontSize: 26,
                fontWeight: 500,
                letterSpacing: "-0.02em",
                marginTop: 8,
                fontVariantNumeric: "tabular-nums",
              }}
            >
              {value}
            </div>
            <div style={{ fontSize: 11.5, color: "#A6A6A0", marginTop: 4 }}>{subLabel}</div>
          </div>
        ))}
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "var(--usage-chart-columns)", gap: 14, marginTop: 14 }}>
        <div
          style={{
            background: "#FFFFFF",
            border: "1px solid #E5E5E1",
            borderRadius: 14,
            padding: 20,
          }}
        >
          <div style={{ fontSize: 13, fontWeight: 600 }}>Minutes used</div>
          <div
            style={{
              display: "flex",
              alignItems: "flex-end",
              gap: buckets.length > 12 ? 4 : 10,
              height: 150,
              marginTop: 18,
            }}
          >
            {buckets.map((bucket, index) => {
              const minutes = bucket.billed_seconds / 60;
              return (
                <div
                  key={bucket.start}
                  onMouseEnter={() => setBarTip(index)}
                  onMouseLeave={() => setBarTip(-1)}
                  style={{
                    flex: 1,
                    display: "flex",
                    flexDirection: "column",
                    alignItems: "center",
                    justifyContent: "flex-end",
                    gap: 7,
                    height: "100%",
                    position: "relative",
                    minWidth: 0,
                  }}
                >
                  <div
                    style={{
                      position: "absolute",
                      top: 0,
                      left: "50%",
                      transform: "translateX(-50%)",
                      background: "#111110",
                      color: "#FFFFFF",
                      fontSize: 11,
                      borderRadius: 6,
                      padding: "4px 8px",
                      whiteSpace: "nowrap",
                      display: barTip === index ? "block" : "none",
                      zIndex: 5,
                    }}
                  >
                    {minutes.toFixed(1)} min
                  </div>
                  <div
                    style={{
                      width: "100%",
                      maxWidth: 34,
                      height: minutes > 0 ? Math.max(Math.round((minutes / maxMinutes) * 122), 8) : 3,
                      background: "#4A6CF7",
                      borderRadius: "4px 4px 2px 2px",
                      opacity: minutes > 0 ? 1 : 0.35,
                    }}
                  />
                  <div
                    style={{
                      fontSize: 10.5,
                      color: "#A6A6A0",
                      whiteSpace: "nowrap",
                      height: 14,
                    }}
                  >
                    {index % Math.ceil(buckets.length / 8) === 0
                      ? bucketLabel(bucket.start, window_)
                      : ""}
                  </div>
                </div>
              );
            })}
          </div>
        </div>

        <div
          style={{
            background: "#FFFFFF",
            border: "1px solid #E5E5E1",
            borderRadius: 14,
            padding: 20,
          }}
        >
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
            <div style={{ fontSize: 13, fontWeight: 600 }}>Wallet charges over time (mock credit)</div>
            <div style={{ fontSize: 11, color: "#A6A6A0", fontVariantNumeric: "tabular-nums" }}>
              peak ₹{maxCost.toFixed(2)}
            </div>
          </div>
          <div style={{ position: "relative", marginTop: 18 }}>
            <svg
              viewBox="0 0 520 150"
              preserveAspectRatio="none"
              style={{ width: "100%", height: 150, display: "block" }}
            >
              <line x1="0" y1="24" x2="520" y2="24" stroke="#F0F0EC" strokeWidth="1" />
              <line x1="0" y1="83" x2="520" y2="83" stroke="#F0F0EC" strokeWidth="1" />
              <line x1="0" y1="142" x2="520" y2="142" stroke="#F0F0EC" strokeWidth="1" />
              <polyline
                points={linePoints}
                fill="none"
                stroke="#4A6CF7"
                strokeWidth="2"
                strokeLinejoin="round"
                strokeLinecap="round"
              />
            </svg>
            <div style={{ position: "absolute", inset: 0, display: "flex" }}>
              {buckets.map((bucket, index) => (
                <div
                  key={bucket.start}
                  onMouseEnter={() => setLineTip(index)}
                  onMouseLeave={() => setLineTip(-1)}
                  style={{ flex: 1, position: "relative" }}
                >
                  <div
                    style={{
                      position: "absolute",
                      top: 2,
                      left: "50%",
                      transform: "translateX(-50%)",
                      background: "#111110",
                      color: "#FFFFFF",
                      fontSize: 11,
                      borderRadius: 6,
                      padding: "4px 8px",
                      whiteSpace: "nowrap",
                      display: lineTip === index ? "block" : "none",
                      zIndex: 5,
                    }}
                  >
                    {formatRupees(bucket.cost_paise)} · {bucketLabel(bucket.start, window_)}
                  </div>
                  <div
                    style={{
                      position: "absolute",
                      top: 0,
                      bottom: 0,
                      left: "50%",
                      width: 1,
                      background: "#E5E5E1",
                      display: lineTip === index ? "block" : "none",
                    }}
                  />
                </div>
              ))}
            </div>
          </div>
          <div
            style={{
              display: "flex",
              justifyContent: "space-between",
              fontSize: 10.5,
              color: "#A6A6A0",
              marginTop: 6,
            }}
          >
            <span>{buckets.length ? bucketLabel(buckets[0].start, window_) : ""}</span>
            <span>
              {buckets.length ? bucketLabel(buckets[buckets.length - 1].start, window_) : ""}
            </span>
          </div>
        </div>
      </div>

      <div
        style={{
          background: "#FFFFFF",
          border: "1px solid #E5E5E1",
          borderRadius: 14,
          marginTop: 14,
          padding: "22px 24px",
        }}
      >
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "baseline",
            gap: 16,
            flexWrap: "wrap",
          }}
        >
          <div style={{ fontSize: 13.5, fontWeight: 600 }}>Latency percentiles</div>
          <div style={{ fontSize: 11, color: "#6B6B66" }}>
            {data.latency.valid_turns} valid turns · {data.latency.excluded_turns} excluded ·
            p99 needs ≥ 100 turns
          </div>
        </div>
        {eligibleStages.length === 0 ? (
          <div style={{ padding: "24px 0 8px", fontSize: 12.5, color: "#A6A6A0" }}>
            No turn metrics in this window yet — make a call and check back.
          </div>
        ) : (
          <div style={{ marginTop: 10 }}>
            {eligibleStages.map((stage) => {
              const name =
                LATENCY_STAGES.find((item) => item.key === stage.key)?.name ??
                (stage.key === "e2e_voice_to_voice_ms"
                  ? "End-to-end"
                  : stage.key === "tts_socket_acquire_ms"
                    ? "TTS socket acquire"
                    : stage.key === "barge_in_stop_ack_ms"
                      ? "Barge-in ack proxy"
                      : stage.key);
              const scale = Math.max(...eligibleStages.map((item) => item.p95 ?? 0), 1000);
              return (
                <div
                  key={stage.key}
                  style={{
                    display: "grid",
                    gridTemplateColumns: "var(--usage-stage-columns)",
                    gap: 16,
                    alignItems: "center",
                    padding: "11px 0",
                    borderBottom: "1px solid #F4F4F0",
                  }}
                >
                  <div style={{ fontSize: 13 }}>{name}</div>
                  <div className="usage-stage-bars"
                    style={{
                      position: "relative",
                      height: 26,
                      background: "#F7F7F5",
                      borderRadius: 6,
                    }}
                  >
                    {stage.p50 != null && (
                      <div
                        style={{
                          position: "absolute",
                          left: 0,
                          top: 4,
                          height: 8,
                          width: `${Math.min((stage.p50 / scale) * 100, 100).toFixed(1)}%`,
                          background: "#111110",
                          borderRadius: "0 4px 4px 0",
                        }}
                      />
                    )}
                    {stage.p95 != null && (
                      <div
                        style={{
                          position: "absolute",
                          left: 0,
                          bottom: 4,
                          height: 8,
                          width: `${Math.min((stage.p95 / scale) * 100, 100).toFixed(1)}%`,
                          background: "#4A6CF7",
                          borderRadius: "0 4px 4px 0",
                        }}
                      />
                    )}
                  </div>
                  <div
                    style={{
                      fontSize: 12,
                      color: "#6B6B66",
                      fontVariantNumeric: "tabular-nums",
                      whiteSpace: "nowrap",
                    }}
                  >
                    p50 {stage.p50 == null ? "—" : `${Math.round(stage.p50)} ms`} · p95{" "}
                    {stage.p95 == null ? "—" : `${Math.round(stage.p95)} ms`} · p99{" "}
                    {stage.p99 == null ? "—" : `${Math.round(stage.p99)} ms`} · n={stage.count}
                  </div>
                </div>
              );
            })}
            <div
              style={{
                display: "flex",
                alignItems: "center",
                gap: 16,
                fontSize: 11,
                color: "#6B6B66",
                marginTop: 10,
              }}
            >
              <span style={{ display: "inline-flex", alignItems: "center", gap: 5 }}>
                <span style={{ width: 10, height: 6, background: "#111110", borderRadius: 2 }} />
                p50
              </span>
              <span style={{ display: "inline-flex", alignItems: "center", gap: 5 }}>
                <span style={{ width: 10, height: 6, background: "#4A6CF7", borderRadius: 2 }} />
                p95
              </span>
            </div>
          </div>
        )}
      </div>

      <div
        style={{
          background: "#FFFFFF",
          border: "1px solid #E5E5E1",
          borderRadius: 14,
          marginTop: 14,
          overflow: "hidden",
        }}
      >
        <div style={{ padding: "18px 20px 12px", fontSize: 13.5, fontWeight: 600 }}>Sessions</div>
        {data.sessions.length === 0 ? (
          <div
            style={{ padding: "36px 20px", textAlign: "center", fontSize: 12.5, color: "#A6A6A0" }}
          >
            No sessions in this window.
          </div>
        ) : (
          <><div className="table-scroll-hint">Swipe to see complete session details →</div><div className="table-scroll"><table style={{ width: "100%", borderCollapse: "collapse" }}>
            <thead>
              <tr>
                <th style={{ ...HEADER_CELL, padding: "8px 20px" }}>Started</th>
                <th style={HEADER_CELL}>Duration</th>
                <th style={HEADER_CELL}>Language</th>
                <th style={HEADER_CELL}>Turns</th>
                <th style={{ ...HEADER_CELL, textAlign: "right" }}>Wallet charge</th>
                <th style={{ ...HEADER_CELL, padding: "8px 20px" }}>End reason</th>
              </tr>
            </thead>
            <tbody>
              {data.sessions.map((sessionRow) => (
                <tr
                  key={sessionRow.id}
                  className="hovRow"
                  onClick={() =>
                    void getSessionDetail(token, sessionRow.id)
                      .then(setDetail)
                      .catch((error: unknown) => pushToast(String(error)))
                  }
                  style={{ cursor: "pointer" }}
                >
                  <td
                    style={{
                      padding: "11px 20px",
                      fontSize: 13,
                      borderBottom: "1px solid #F4F4F0",
                      whiteSpace: "nowrap",
                    }}
                  >
                    {formatDateTime(sessionRow.started_at)}
                  </td>
                  <td
                    style={{
                      padding: "11px 12px",
                      fontSize: 13,
                      fontVariantNumeric: "tabular-nums",
                      borderBottom: "1px solid #F4F4F0",
                    }}
                  >
                    {formatLongDuration(sessionRow.billed_seconds)}
                  </td>
                  <td
                    style={{
                      padding: "11px 12px",
                      fontSize: 12,
                      fontFamily: "ui-monospace,'SF Mono',monospace",
                      color: "#6B6B66",
                      borderBottom: "1px solid #F4F4F0",
                    }}
                  >
                    {sessionRow.languages.length ? sessionRow.languages.join(" · ") : "—"}
                  </td>
                  <td
                    style={{
                      padding: "11px 12px",
                      fontSize: 13,
                      fontVariantNumeric: "tabular-nums",
                      borderBottom: "1px solid #F4F4F0",
                    }}
                  >
                    {sessionRow.turns}
                    {sessionRow.interrupted_turns > 0 && (
                      <span style={{ color: "#9A6A0B", fontSize: 11.5 }}>
                        {" "}
                        · {sessionRow.interrupted_turns} ⏸
                      </span>
                    )}
                  </td>
                  <td
                    style={{
                      padding: "11px 12px",
                      fontSize: 13,
                      textAlign: "right",
                      fontVariantNumeric: "tabular-nums",
                      borderBottom: "1px solid #F4F4F0",
                    }}
                  >
                    {formatRupees(sessionRow.cost_paise)}
                  </td>
                  <td style={{ padding: "11px 20px", borderBottom: "1px solid #F4F4F0" }}>
                    {reasonPill(sessionRow.end_reason)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table></div></>
        )}
      </div>

      {detail && (
        <>
          <button type="button" aria-label="Close session detail"
            onClick={() => setDetail(null)}
            style={{ position: "fixed", inset: 0, border: 0, borderRadius: 0, background: "rgba(17,17,16,0.18)", zIndex: 60 }}
          />
          <div className="usage-detail-drawer" role="dialog" aria-modal="true" aria-label="Session detail"
            style={{
              position: "fixed",
              top: 0,
              right: 0,
              bottom: 0,
              width: 380,
              maxWidth: "88vw",
              background: "#FFFFFF",
              borderLeft: "1px solid #E5E5E1",
              zIndex: 61,
              padding: 24,
              boxSizing: "border-box",
              overflowY: "auto",
              boxShadow: "-28px 0 56px -36px rgba(17,17,16,0.3)",
            }}
          >
            <div
              style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}
            >
              <div>
                <div style={{ fontSize: 15, fontWeight: 600 }}>Session detail</div>
                <div
                  style={{
                    fontSize: 11.5,
                    fontFamily: "ui-monospace,'SF Mono',monospace",
                    color: "#A6A6A0",
                    marginTop: 3,
                  }}
                >
                  {detail.session.id.slice(0, 8)}
                </div>
              </div>
              <button
                type="button"
                onClick={() => setDetail(null)}
                title="Close"
                className="hovFg"
                style={{
                  border: "none",
                  background: "none",
                  color: "#6B6B66",
                  fontSize: 18,
                  padding: "2px 6px",
                }}
              >
                ×
              </button>
            </div>
            <div
              style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 14, marginTop: 22 }}
            >
              {(
                [
                  ["Started", formatDateTime(detail.session.started_at)],
                  ["Duration", formatLongDuration(detail.session.billed_seconds)],
                  [
                    "Language",
                    detail.session.languages.length
                      ? detail.session.languages.join(" · ")
                      : "—",
                  ],
                  ["Wallet charge (mock)", formatRupees(detail.session.cost_paise)],
                ] as Array<[string, string]>
              ).map(([label, value]) => (
                <div key={label}>
                  <div
                    style={{
                      fontSize: 10.5,
                      fontWeight: 600,
                      letterSpacing: "0.08em",
                      textTransform: "uppercase",
                      color: "#A6A6A0",
                    }}
                  >
                    {label}
                  </div>
                  <div style={{ fontSize: 13.5, marginTop: 4, fontVariantNumeric: "tabular-nums" }}>
                    {value}
                  </div>
                </div>
              ))}
              <div>
                <div
                  style={{
                    fontSize: 10.5,
                    fontWeight: 600,
                    letterSpacing: "0.08em",
                    textTransform: "uppercase",
                    color: "#A6A6A0",
                  }}
                >
                  End reason
                </div>
                <div style={{ marginTop: 5 }}>{reasonPill(detail.session.end_reason)}</div>
              </div>
            </div>
            <div style={{ height: 1, background: "#F0F0EC", margin: "22px 0" }} />
            <div style={{ fontSize: 12, fontWeight: 600 }}>Turns</div>
            {detail.turns.length === 0 ? (
              <div style={{ marginTop: 10, fontSize: 12.5, color: "#A6A6A0" }}>
                No turn metrics recorded for this session.
              </div>
            ) : (
              <div style={{ marginTop: 10, display: "flex", flexDirection: "column", gap: 4 }}>
                {detail.turns.map((turn) => {
                  const total = LATENCY_STAGES.reduce(
                    (sum, stage) => sum + (turn.stages[stage.key] ?? 0),
                    0,
                  );
                  return (
                    <div
                      key={turn.turn_index}
                      style={{
                        display: "grid",
                        gridTemplateColumns: "58px 1fr 66px",
                        gap: 10,
                        alignItems: "center",
                        border: "1px solid #F0F0EC",
                        borderRadius: 8,
                        padding: "8px 10px",
                      }}
                    >
                      <div>
                        <div style={{ fontSize: 11.5, fontWeight: 600 }}>
                          Turn {turn.turn_index + 1}
                          {turn.interrupted && <span style={{ color: "#9A6A0B" }}> ⏸</span>}
                        </div>
                        <div
                          style={{
                            fontSize: 10,
                            fontFamily: "ui-monospace,'SF Mono',monospace",
                            color: "#A6A6A0",
                            marginTop: 1,
                          }}
                        >
                          {turn.language_code ?? "—"}
                        </div>
                      </div>
                      <div
                        style={{
                          display: "flex",
                          height: 8,
                          borderRadius: 4,
                          overflow: "hidden",
                          background: "#F7F7F5",
                        }}
                      >
                        {total > 0 &&
                          LATENCY_STAGES.map((stage) => {
                            const value = turn.stages[stage.key];
                            if (value == null || value <= 0) return null;
                            return (
                              <div
                                key={stage.key}
                                style={{
                                  width: `${((value / total) * 100).toFixed(1)}%`,
                                  background: stage.color,
                                  flexShrink: 0,
                                }}
                              />
                            );
                          })}
                      </div>
                      <div
                        style={{
                          fontSize: 11.5,
                          fontFamily: "ui-monospace,'SF Mono',monospace",
                          fontWeight: 600,
                          textAlign: "right",
                          color:
                            turn.e2e_voice_to_voice_ms != null &&
                            turn.e2e_voice_to_voice_ms > 1500
                              ? "#B3352E"
                              : "#111110",
                        }}
                      >
                        {turn.e2e_voice_to_voice_ms == null
                          ? "—"
                          : `${Math.round(turn.e2e_voice_to_voice_ms)} ms`}
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        </>
      )}
    </div>
  );
}

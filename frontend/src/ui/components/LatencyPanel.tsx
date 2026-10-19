import { useState } from "react";

import { formatDuration } from "../format";
import { e2eColor, LATENCY_STAGES, turnE2e, turnNumber, waterfallStages } from "../latency";
import type { TimedTurnMetrics } from "../hooks/useVoiceSession";

type LatencyPanelProps = {
  turns: TimedTurnMetrics[];
  onCollapse: () => void;
};

// Docked to the left edge of the call screen, mirroring the todos panel on
// the right; the header chevron (or the header "Latency" button) collapses it.
export function LatencyPanel({ turns, onCollapse }: LatencyPanelProps) {
  // null follows the newest turn; clicking a row pins that specific turn.
  const [pinnedTurnId, setPinnedTurnId] = useState<string | null>(null);
  const selectedTurn =
    (pinnedTurnId != null
      ? turns.find((turn) => turn.metric.turn_id === pinnedTurnId)
      : null) ??
    turns[0] ??
    null;
  const e2es = turns
    .map((turn) => turnE2e(turn.metric))
    .filter((value): value is number => value != null);
  const average = e2es.length
    ? Math.round(e2es.reduce((total, value) => total + value, 0) / e2es.length)
    : null;

  const selectedE2e = selectedTurn ? turnE2e(selectedTurn.metric) : null;
  const stages = selectedTurn ? waterfallStages(selectedTurn.metric.stages) : [];
  const lastStage = stages.at(-1);
  const stageSpan = lastStage ? lastStage.startMs + (lastStage.durationMs ?? 0) : 0;
  const scale = Math.max(selectedE2e ?? stageSpan, 500);
  const interrupted = selectedTurn?.metric.dimensions.interrupted === true;
  const bargeAck = selectedTurn?.metric.derived.barge_in_stop_ack_ms ?? null;

  return (
    <aside className="call-side-panel latency-panel" aria-label="Latency details"
      style={{
        width: 360,
        flexShrink: 0,
        borderRight: "1px solid #E5E5E1",
        background: "#FFFFFF",
        display: "flex",
        flexDirection: "column",
        minHeight: 0,
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          padding: "16px 18px 12px",
          flexShrink: 0,
        }}
      >
        <div>
          <div style={{ fontSize: 13, fontWeight: 600 }}>Latency waterfall</div>
          <div
            style={{
              fontSize: 10.5,
              fontFamily: "ui-monospace,'SF Mono',monospace",
              color: "#A6A6A0",
              marginTop: 3,
            }}
          >
            turn_metrics · end of speech → first audio
          </div>
        </div>
        <button
          type="button"
          onClick={onCollapse}
          title="Collapse panel"
          className="hovFg"
          style={{
            border: "none",
            background: "none",
            color: "#6B6B66",
            fontSize: 15,
            padding: "2px 6px",
          }}
        >
          ‹
        </button>
      </div>

      <div style={{ flex: 1, overflowY: "auto", padding: "4px 18px 18px" }}>
          {turns.length === 0 && (
            <div
              style={{
                padding: "40px 0",
                textAlign: "center",
                fontSize: 12.5,
                color: "#A6A6A0",
                lineHeight: 1.6,
              }}
            >
              No turns yet.
              <br />
              Metrics appear after the agent's first reply.
            </div>
          )}

          {selectedTurn && (
            <>
              <div
                style={{
                  display: "flex",
                  alignItems: "baseline",
                  justifyContent: "space-between",
                }}
              >
                <div style={{ fontSize: 12, fontWeight: 600, color: "#6B6B66" }}>
                  Turn {turnNumber(selectedTurn.metric.turn_id)} · at{" "}
                  {formatDuration(selectedTurn.atSeconds)}
                </div>
                <div
                  style={{
                    fontSize: 20,
                    fontWeight: 600,
                    fontVariantNumeric: "tabular-nums",
                    color: e2eColor(selectedE2e),
                  }}
                >
                  {selectedE2e == null ? "—" : `${Math.round(selectedE2e)} ms`}
                  <span style={{ fontSize: 11.5, fontWeight: 500, color: "#A6A6A0" }}> e2e</span>
                </div>
              </div>

              {interrupted && (
                <div
                  style={{
                    marginTop: 8,
                    fontSize: 11.5,
                    color: "#9A6A0B",
                    borderLeft: "2px solid #EED9A8",
                    paddingLeft: 8,
                  }}
                >
                  interrupted — ack proxy{" "}
                  {bargeAck == null ? "n/a" : `${bargeAck.toFixed(1)} ms`}
                </div>
              )}

              <div style={{ marginTop: 12 }}>
                {stages.map((stage) => (
                  <div
                    key={stage.key}
                    style={{
                      display: "grid",
                      gridTemplateColumns: "118px 1fr 62px",
                      gap: 10,
                      alignItems: "center",
                      padding: "5px 0",
                    }}
                  >
                    <div
                      style={{
                        fontSize: 11,
                        fontWeight: 500,
                        color: "#6B6B66",
                        whiteSpace: "nowrap",
                        overflow: "hidden",
                        textOverflow: "ellipsis",
                      }}
                    >
                      {stage.name}
                    </div>
                    <div
                      style={{
                        position: "relative",
                        height: 16,
                        background: "#F7F7F5",
                        borderRadius: 4,
                        overflow: "hidden",
                      }}
                    >
                      {stage.durationMs != null && (
                        <div
                          style={{
                            position: "absolute",
                            top: 2,
                            bottom: 2,
                            left: `${((stage.startMs / scale) * 100).toFixed(1)}%`,
                            width: `${Math.max((stage.durationMs / scale) * 100, 1).toFixed(1)}%`,
                            background: stage.color,
                            borderRadius: 3,
                          }}
                        />
                      )}
                    </div>
                    <div
                      style={{
                        fontSize: 11,
                        fontFamily: "ui-monospace,'SF Mono',monospace",
                        color: "#6B6B66",
                        textAlign: "right",
                        fontVariantNumeric: "tabular-nums",
                      }}
                    >
                      {stage.durationMs == null ? "n/a" : `${Math.round(stage.durationMs)} ms`}
                    </div>
                  </div>
                ))}
                <div
                  style={{
                    display: "grid",
                    gridTemplateColumns: "118px 1fr 62px",
                    gap: 10,
                    marginTop: 2,
                  }}
                >
                  <div />
                  <div
                    style={{
                      display: "flex",
                      justifyContent: "space-between",
                      fontSize: 10,
                      fontFamily: "ui-monospace,'SF Mono',monospace",
                      color: "#C0C0BA",
                    }}
                  >
                    <span>0</span>
                    <span>{Math.round(scale)} ms</span>
                  </div>
                  <div />
                </div>
              </div>

              <div
                style={{
                  display: "grid",
                  gridTemplateColumns: "repeat(4,1fr)",
                  gap: 8,
                  marginTop: 18,
                  paddingTop: 16,
                  borderTop: "1px solid #F0F0EC",
                }}
              >
                {[
                  ["Turns", String(turns.length)],
                  ["Avg", average == null ? "—" : `${average} ms`],
                  ["Min", e2es.length ? `${Math.round(Math.min(...e2es))} ms` : "—"],
                  ["Max", e2es.length ? `${Math.round(Math.max(...e2es))} ms` : "—"],
                ].map(([label, value]) => (
                  <div key={label} style={{ textAlign: "center" }}>
                    <div
                      style={{
                        fontSize: 10,
                        fontWeight: 600,
                        letterSpacing: "0.08em",
                        textTransform: "uppercase",
                        color: "#A6A6A0",
                      }}
                    >
                      {label}
                    </div>
                    <div
                      style={{
                        fontSize: 14,
                        fontFamily: "ui-monospace,'SF Mono',monospace",
                        marginTop: 3,
                      }}
                    >
                      {value}
                    </div>
                  </div>
                ))}
              </div>

              <div
                style={{
                  fontSize: 11,
                  fontWeight: 600,
                  letterSpacing: "0.07em",
                  textTransform: "uppercase",
                  color: "#A6A6A0",
                  marginTop: 20,
                }}
              >
                Recent turns
              </div>
              <div style={{ marginTop: 8, display: "flex", flexDirection: "column", gap: 4 }}>
                {turns.map((turn) => {
                  const e2e = turnE2e(turn.metric);
                  const rowStages = waterfallStages(turn.metric.stages);
                  const total = rowStages.reduce(
                    (sum, stage) => sum + (stage.durationMs ?? 0),
                    0,
                  );
                  const isSelected =
                    selectedTurn?.metric.turn_id === turn.metric.turn_id;
                  return (
                    <button
                      key={turn.metric.turn_id}
                      type="button"
                      onClick={() => setPinnedTurnId(turn.metric.turn_id)}
                      style={{
                        display: "grid",
                        gridTemplateColumns: "72px 1fr 70px",
                        gap: 10,
                        alignItems: "center",
                        width: "100%",
                        border: `1px solid ${isSelected ? "#B9B9B2" : "#F0F0EC"}`,
                        background: isSelected ? "#F7F7F5" : "#FFFFFF",
                        borderRadius: 8,
                        padding: "8px 10px",
                        textAlign: "left",
                      }}
                    >
                      <div>
                        <div style={{ fontSize: 11.5, fontWeight: 600 }}>
                          Turn {turnNumber(turn.metric.turn_id)}
                          {turn.metric.dimensions.interrupted === true && (
                            <span style={{ color: "#9A6A0B" }}> ⏸</span>
                          )}
                        </div>
                        <div
                          style={{
                            fontSize: 10,
                            fontFamily: "ui-monospace,'SF Mono',monospace",
                            color: "#A6A6A0",
                            marginTop: 1,
                          }}
                        >
                          {formatDuration(turn.atSeconds)}
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
                          rowStages.map(
                            (stage) =>
                              stage.durationMs != null && (
                                <div
                                  key={stage.key}
                                  style={{
                                    width: `${((stage.durationMs / total) * 100).toFixed(1)}%`,
                                    background: stage.color,
                                    flexShrink: 0,
                                  }}
                                />
                              ),
                          )}
                      </div>
                      <div
                        style={{
                          fontSize: 11.5,
                          fontFamily: "ui-monospace,'SF Mono',monospace",
                          fontWeight: 600,
                          textAlign: "right",
                          color: e2eColor(e2e),
                        }}
                      >
                        {e2e == null ? "—" : `${Math.round(e2e)} ms`}
                      </div>
                    </button>
                  );
                })}
              </div>

              <div style={{ display: "flex", flexWrap: "wrap", gap: "10px 14px", marginTop: 16 }}>
                {LATENCY_STAGES.map((stage) => (
                  <span
                    key={stage.key}
                    style={{
                      display: "inline-flex",
                      alignItems: "center",
                      gap: 5,
                      fontSize: 10.5,
                      color: "#6B6B66",
                    }}
                  >
                    <span
                      style={{
                        width: 9,
                        height: 9,
                        borderRadius: 3,
                        background: stage.color,
                      }}
                    />
                    {stage.name}
                  </span>
                ))}
              </div>
            </>
          )}
      </div>
    </aside>
  );
}

import { formatDuration } from "../format";
import type { TranscriptEntry } from "../hooks/useVoiceSession";
import { spokenText } from "../conversation";

type TranscriptDrawerProps = {
  sessionLabel: string;
  log: TranscriptEntry[];
  onClose: () => void;
};

const WHO_LABEL: Record<TranscriptEntry["who"], [string, string]> = {
  user: ["You", "#6B6B66"],
  agent: ["Agent", "#3A57D4"],
  system: ["System", "#9A6A0B"],
};

export function TranscriptDrawer({ sessionLabel, log, onClose }: TranscriptDrawerProps) {
  return (
    <>
      <button type="button" className="transcript-backdrop" aria-label="Close transcript"
        onClick={onClose}
        style={{ position: "fixed", inset: 0, border: 0, borderRadius: 0, background: "rgba(17,17,16,0.18)", zIndex: 70 }}
      />
      <div className="transcript-drawer" role="dialog" aria-modal="true" aria-label="Call transcript"
        style={{
          position: "fixed",
          top: 0,
          right: 0,
          bottom: 0,
          width: 380,
          maxWidth: "88vw",
          background: "#FFFFFF",
          borderLeft: "1px solid #E5E5E1",
          zIndex: 71,
          display: "flex",
          flexDirection: "column",
          boxShadow: "-28px 0 56px -36px rgba(17,17,16,0.3)",
        }}
      >
        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            padding: "20px 24px 12px",
          }}
        >
          <div>
            <div style={{ fontSize: 15, fontWeight: 600 }}>Call transcript</div>
            <div
              style={{
                fontSize: 11.5,
                fontFamily: "ui-monospace,'SF Mono',monospace",
                color: "#A6A6A0",
                marginTop: 3,
              }}
            >
              {sessionLabel}
            </div>
          </div>
          <button
            type="button"
            onClick={onClose}
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
        <div style={{ flex: 1, overflowY: "auto", padding: "4px 24px 24px" }}>
          {log.length === 0 && (
            <div
              style={{
                padding: "32px 0",
                textAlign: "center",
                fontSize: 12.5,
                color: "#A6A6A0",
              }}
            >
              Nothing yet — say something and it will appear here.
            </div>
          )}
          {log.map((entry, index) => {
            if (!spokenText(entry)) return null;
            const [who, whoColor] = WHO_LABEL[entry.who];
            return (
              <div key={index} style={{ padding: "10px 0", borderBottom: "1px solid #F4F4F0" }}>
                <div style={{ display: "flex", alignItems: "baseline", gap: 8 }}>
                  <span
                    style={{
                      fontSize: 10.5,
                      fontWeight: 600,
                      letterSpacing: "0.06em",
                      textTransform: "uppercase",
                      color: whoColor,
                    }}
                  >
                    {who}
                  </span>
                  <span
                    style={{
                      fontSize: 10.5,
                      fontFamily: "ui-monospace,'SF Mono',monospace",
                      color: "#A6A6A0",
                    }}
                  >
                    {entry.languageCode ? `${entry.languageCode} · ` : ""}
                    {formatDuration(entry.atSeconds)}
                  </span>
                </div>
                <div
                  style={{
                    fontSize: entry.who === "system" ? 12 : 13.5,
                    lineHeight: 1.55,
                    marginTop: 4,
                    color: entry.who === "system" ? "#6B6B66" : "#111110",
                    fontFamily:
                      entry.who === "system"
                        ? "ui-monospace,'SF Mono',monospace"
                        : undefined,
                  }}
                >
                  {spokenText(entry).replaceAll("**", "")}
                </div>
                {entry.interrupted && (
                  <div
                    style={{
                      marginTop: 5,
                      fontSize: 11.5,
                      color: "#9A6A0B",
                      borderLeft: "2px solid #EED9A8",
                      paddingLeft: 8,
                    }}
                  >
                    ⏸ interrupted — user started speaking
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </div>
    </>
  );
}

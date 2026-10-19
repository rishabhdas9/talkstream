import { useEffect, useMemo, useRef, useState } from "react";

import { formatDuration, formatRupees } from "../format";
import { e2eColor, turnE2e } from "../latency";
import type { useVoiceSession } from "../hooks/useVoiceSession";
import { actionReceiptText, callRecap, conversationTimeline, richWords, spokenText, type ActionReceipt } from "../conversation";
import type { TranscriptEntry } from "../hooks/useVoiceSession";
import type { SearchSource } from "../../voice/protocol";
import { LatencyPanel } from "../components/LatencyPanel";
import { TranscriptDrawer } from "../components/TranscriptDrawer";
import { TodosPanel } from "../components/TodosPanel";

type CallPageProps = {
  session: ReturnType<typeof useVoiceSession>;
  goWallet: () => void;
  walletBalancePaise: number | null;
};

const WAVE_BARS = Array.from({ length: 19 }, (_, index) =>
  Math.max(13, Math.round(35 + Math.sin(index * 0.9) * 16 + Math.sin(index * 0.38 + 1.3) * 20)),
);

const ENDED_COPY: Record<string, [string, string]> = {
  user: ["Call ended", "Your conversation remains visible below."],
  balance_exhausted: ["Balance exhausted", "The call stopped when your wallet reached ₹0.00."],
  error: ["Connection error", "Something went wrong in the voice pipeline. You were charged only for connected time."],
  disconnected: ["Connection lost", "The voice channel closed unexpectedly. You were charged only for connected time."],
  provider_budget_exhausted: ["AI allowance reached", "This call used its AI spending allowance. Start a new call to continue."],
  session_expired: ["Time limit reached", "Calls end at the time limit. Start a new call to continue."],
  provider_failures: ["Provider unavailable", "The call stopped after repeated provider failures. You were charged only for connected time."],
  cleanup_incomplete: ["Call stopped", "The call could not shut down cleanly. You were charged only for connected time."],
};

function ToolReceipt({ receipt }: { receipt: ActionReceipt }) {
  return (
    <div className={`call-receipt call-receipt--${receipt.status}`}>
      <span className="call-receipt-dot" aria-hidden="true" />
      <span className="call-receipt-title">{actionReceiptText(receipt)}</span>
      <span className="call-receipt-status">{receipt.status === "confirmed" ? "Confirmed by tool" : receipt.status === "failed" ? "Tool reported failure" : "Outcome unconfirmed"}</span>
    </div>
  );
}

function hostOf(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return url;
  }
}

function SourceCards({ sources }: { sources: SearchSource[] }) {
  return (
    <ul className="call-sources" aria-label="Web sources">
      {sources.map((source) => (
        <li key={source.url}>
          <a className="call-source" href={source.url} target="_blank" rel="noopener noreferrer">
            <span className="call-source-media">
              {source.image ? (
                <img src={source.image} alt="" loading="lazy" referrerPolicy="no-referrer"
                  onError={(event) => { event.currentTarget.hidden = true; }} />
              ) : null}
              <span className="call-source-initial" aria-hidden="true">{hostOf(source.url).charAt(0)}</span>
            </span>
            <span className="call-source-title">{source.title}</span>
            <span className="call-source-host">
              {source.favicon && <img src={source.favicon} alt="" loading="lazy" referrerPolicy="no-referrer"
                onError={(event) => { event.currentTarget.hidden = true; }} />}
              {hostOf(source.url)}
            </span>
          </a>
        </li>
      ))}
    </ul>
  );
}

function AgentWords({ turn }: { turn: TranscriptEntry }) {
  const text = spokenText(turn);
  const speaking = turn.spokenChars != null && !turn.interrupted;
  if (!text && speaking) {
    return <p className="call-typing" aria-label="Preparing answer"><span /><span /><span /></p>;
  }
  if (!text) return null;
  // Each word mounts once, so only newly spoken words fade in.
  return (
    <p className={speaking ? "call-speaking" : undefined}>
      {richWords(text).map(({ word, bold }, index) => (
        <span key={index} className={speaking ? "call-word" : undefined}>
          {index ? " " : ""}{bold ? <strong>{word}</strong> : word}
        </span>
      ))}
      {speaking && <span className="call-caret" aria-hidden="true" />}
    </p>
  );
}

export function CallPage({ session, goWallet, walletBalancePaise }: CallPageProps) {
  const { state, start, stop, setMuted, runTodoTool } = session;
  const [panel, setPanel] = useState<"latency" | "todos" | null>(null);
  const [logOpen, setLogOpen] = useState(false);
  const feedEndRef = useRef<HTMLDivElement>(null);
  const status = state.callStatus;
  const muted = state.muted;
  const balance = state.balancePaise ?? walletBalancePaise;
  const balanceLow = state.lowBalance || (balance != null && balance < 100);
  const latestE2e = state.metrics[0] ? turnE2e(state.metrics[0].metric) : null;
  const timeline = useMemo(() => conversationTimeline(state.log, state.actionReceipts), [state.log, state.actionReceipts]);
  const recap = useMemo(() => callRecap(state.log, state.actionReceipts), [state.log, state.actionReceipts]);
  const recapParts = [
    `${recap.spokenTurns} transcribed ${recap.spokenTurns === 1 ? "turn" : "turns"}`,
    ...(recap.confirmedActions ? [`${recap.confirmedActions} confirmed tool ${recap.confirmedActions === 1 ? "outcome" : "outcomes"}`] : []),
    ...(recap.failedActions ? [`${recap.failedActions} failed`] : []),
    ...(recap.unconfirmedActions ? [`${recap.unconfirmedActions} unconfirmed`] : []),
    ...(!state.actionReceipts.length ? ["No tool outcomes reported"] : []),
  ];
  const endedReason = state.endReason ?? "user";
  const [endedTitle, endedSub] = ENDED_COPY[endedReason] ?? ENDED_COPY.user;
  const stateLabel = muted
    ? "Microphone muted"
    : state.agentState === "user_speaking"
      ? "You're speaking"
      : state.agentState === "speaking"
        ? "Assistant speaking"
        : state.agentState === "thinking"
          ? "Assistant thinking"
          : state.agentState === "interrupted"
            ? "Assistant interrupted"
            : "Listening to you";
  const waveAnimation = muted
    ? "waveMuted 3s ease-in-out infinite"
    : state.agentState === "speaking" || state.agentState === "user_speaking"
      ? "waveSpeak 1.1s ease-in-out infinite"
      : state.agentState === "thinking"
        ? "waveThink 1.4s ease-in-out infinite"
        : "waveListen 2.6s ease-in-out infinite";

  useEffect(() => {
    if (status === "active") feedEndRef.current?.scrollIntoView({ block: "nearest" });
  }, [timeline, status]);

  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (document.querySelector(".app-sidebar--open")) return;
      const target = event.target as HTMLElement | null;
      if (target?.closest("input, textarea, select, [contenteditable='true']")) return;
      if (event.key.toLowerCase() === "m" && status === "active") setMuted(!muted);
      if (event.key === "Escape") {
        if (logOpen) setLogOpen(false);
        else if (panel) setPanel(null);
        else if (status === "active" || status === "connecting") void stop();
      }
    }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [status, muted, panel, logOpen, setMuted, stop]);

  function togglePanel(next: "latency" | "todos") {
    setPanel((current) => (current === next ? null : next));
  }

  return (
    <div className="call-page">
      <header className="call-header">
        <div className="call-heading">
          <div className="call-eyebrow">
            VOICE STUDIO {state.sessionId ? `· ${state.sessionId.slice(0, 8)}` : ""}
          </div>
          <div className="call-title-row">
            <h1>{status === "active" || status === "connecting" ? "Live conversation" : status === "ended" ? "Recent conversation" : "New conversation"}</h1>
            {(status === "active" || status === "connecting") && (
              <span className="call-live-badge"><span /> Live</span>
            )}
          </div>
        </div>
        <div className="call-session-meta">
          <span className="call-timer">{formatDuration(state.elapsedSeconds)}</span>
          <span className="call-cost">−{formatRupees(state.sessionCostPaise)}</span>
          <button
            type="button"
            className={`call-balance${balanceLow ? " call-balance--low" : ""}`}
            onClick={goWallet}
            aria-label={`Demo wallet balance ${balance == null ? "unavailable" : formatRupees(balance)}. Open wallet`}
          >
            {balance == null ? "Balance —" : formatRupees(balance)} <span aria-hidden="true">↗</span>
          </button>
        </div>
      </header>

      <div className="call-toolbar">
        <div className="call-toolbar-context">
          {status === "active"
            ? "Voice is the input. Your conversation appears here as you speak."
            : "Speak naturally in English, Hindi, or Hinglish."}
        </div>
        <div className="call-toolbar-actions">
          {status === "active" && state.languageCode && (
            <span className="call-language">{state.languageCode}</span>
          )}
          <button type="button" className="call-tool-button" onClick={() => togglePanel("todos")} aria-expanded={panel === "todos"}>
            Todos <span>{state.todos.length}</span>
          </button>
          <button type="button" className="call-tool-button" onClick={() => togglePanel("latency")} aria-expanded={panel === "latency"}>
            Latency {latestE2e != null && <span style={{ color: e2eColor(latestE2e) }}>{Math.round(latestE2e)} ms</span>}
          </button>
          <button type="button" className="call-tool-button call-transcript-button" onClick={() => setLogOpen(true)}>
            Transcript
          </button>
        </div>
      </div>

      <div className="call-workspace">
        <section className="call-stage" aria-label="Conversation stage">
          {(status === "idle" || status === "connecting" || status === "denied") && (
            <div className="call-intro">
              <div className={`call-intro-mark${status === "connecting" ? " call-intro-mark--connecting" : ""}`} aria-hidden="true">
                <span /><span /><span /><span /><span />
              </div>
              <div className="call-eyebrow">{status === "connecting" ? "GETTING READY" : "YOUR VOICE, IN REAL TIME"}</div>
              <h2>{status === "denied" ? "Microphone access needed" : status === "connecting" ? "Connecting your call" : "A conversation that moves with you."}</h2>
              <p>{status === "denied" ? "Allow microphone access in your browser's address bar, then try again." : status === "connecting" ? "Requesting microphone access and opening a secure channel…" : "Ask a question, search the web, or manage your todos. Talkstream listens as you speak and replies out loud."}</p>
              {status !== "connecting" && (
                <button type="button" className="call-primary-button" onClick={() => void start()}>
                  {status === "denied" ? "Try again" : "Start voice call"} <span aria-hidden="true">↗</span>
                </button>
              )}
              {status === "idle" && (
                <div className="call-intro-note">Microphone permission is requested when you start. Billed per second from your demo wallet.</div>
              )}
              {state.error && <p className="call-error" role="alert">{state.error}</p>}
            </div>
          )}

          {(status === "active" || status === "ended") && (
            <>
              {status === "ended" && (
                <div className="call-ended-banner" role="status">
                  <div>
                    <strong>{endedTitle}</strong>
                    <p>{endedSub} {formatDuration(state.elapsedSeconds)} · {formatRupees(state.sessionCostPaise)} charged to your demo wallet.</p>
                    {state.error && <p className="call-error">{state.error}</p>}
                  </div>
                  <button type="button" className="call-primary-button" onClick={() => void start()}>New call</button>
                </div>
              )}
              {status === "ended" && state.sessionId && (
                <section className="call-recap" aria-label="Call recap">
                  <div className="call-recap-heading">
                    <h2>Call recap</h2>
                    <span>Observed events only · recap not saved</span>
                  </div>
                  <p>{recapParts.join(" · ")}</p>
                </section>
              )}
              <div className="call-conversation" role="log" aria-label="Live conversation">
                <div className="call-conversation-inner">
                  {state.log.length === 0 && (
                    <div className="call-empty-conversation">
                      <div className="call-eyebrow">CONVERSATION</div>
                      <h2>{status === "ended" ? "No spoken turns captured." : "We're listening."}</h2>
                      <p>{status === "ended" ? "This call ended before a transcript appeared." : "Start speaking. Your words and the assistant's response will appear here."}</p>
                      {status === "active" && <span>Try “What will the weather be in Bengaluru tomorrow?”</span>}
                    </div>
                  )}
                  {timeline.map((item, index) => item.kind === "receipt" ? (
                    <ToolReceipt key={`receipt-${item.receipt.callId}`} receipt={item.receipt} />
                  ) : item.turn.who === "system" ? (
                    <div key={`turn-${index}`} className="call-turn call-turn--system"><p>{item.turn.text}</p></div>
                  ) : (
                    <div key={`turn-${index}`} className={`call-turn call-turn--${item.turn.who}${item.turn.spokenChars != null && !item.turn.interrupted ? " call-turn--live" : ""}`}>
                      <div className="call-turn-meta">
                        <span>{item.turn.who === "user" ? "You" : "Talkstream"}</span>
                        <time>{formatDuration(item.turn.atSeconds)}</time>
                        {item.turn.languageCode && <span>{item.turn.languageCode}</span>}
                      </div>
                      {item.turn.who === "agent" ? <AgentWords turn={item.turn} /> : <p>{item.turn.text}</p>}
                      {item.turn.sources?.length ? <SourceCards sources={item.turn.sources} /> : null}
                      {item.turn.interrupted && <div className="call-interrupted">Stopped here when you started speaking</div>}
                    </div>
                  ))}
                  {status === "active" && state.agentState === "thinking" && state.log.at(-1)?.who === "user" && (
                    <div className="call-turn call-turn--agent call-turn--live">
                      <div className="call-turn-meta"><span>Talkstream</span></div>
                      <p className="call-typing" aria-label="Preparing answer"><span /><span /><span /></p>
                    </div>
                  )}
                  <div ref={feedEndRef} />
                </div>
              </div>
              {status === "active" && (
                <div className="call-dock">
                  <div className="call-presence" role="status" aria-live="polite">
                    <div className={`call-presence-dot${muted ? " call-presence-dot--muted" : ""}`} />
                    <div>
                      <strong>{stateLabel}</strong>
                      <span>{muted ? "Tap unmute to continue" : state.transportStatus === "silence_not_transmitting" ? "Silence is not transmitted" : "You can speak over the assistant at any time"}</span>
                    </div>
                  </div>
                  <div className="call-waveform" aria-hidden="true">
                    {WAVE_BARS.map((height, index) => (
                      <span key={index} style={{ height, animation: `${waveAnimation} ${-index * 0.09}s` }} />
                    ))}
                  </div>
                  <div className="call-dock-actions">
                    <button type="button" className={`call-mute-button${muted ? " call-mute-button--muted" : ""}`} onClick={() => setMuted(!muted)} aria-pressed={muted}>
                      {muted ? "Unmute" : "Mute"}<span className="call-shortcut">M</span>
                    </button>
                    <button type="button" className="call-end-button" onClick={() => void stop()}>End call</button>
                  </div>
                </div>
              )}
            </>
          )}
        </section>

        {panel && (
          <>
            <button type="button" className="call-panel-scrim" aria-label="Close panel" onClick={() => setPanel(null)} />
            {panel === "latency" ? (
              <LatencyPanel turns={state.metrics} onCollapse={() => setPanel(null)} />
            ) : (
              <TodosPanel todos={state.todos} agentToast={state.agentTodoToast} error={state.todoError || state.error} onRunTool={runTodoTool} onCollapse={() => setPanel(null)} />
            )}
          </>
        )}
      </div>
      {logOpen && (
        <TranscriptDrawer sessionLabel={state.sessionId ? `session ${state.sessionId.slice(0, 8)}` : ""} log={state.log} onClose={() => setLogOpen(false)} />
      )}
    </div>
  );
}

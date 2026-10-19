import { type FormEvent, useEffect, useLayoutEffect, useRef, useState } from "react";

import { getCurrentUser, getWallet, rechargeWallet, requestOtp, verifyOtp } from "../api/auth";
import {
  AUTHENTICATION_EXPIRED_MESSAGE,
  isAuthenticationExpiredError,
} from "../api/errors";
import { loadTodos, type Todo } from "../store/todos";
import { VoiceClient, type BillingEvent } from "../voice/client";
import type { TurnMetricsEvent } from "../voice/protocol";

const STAGE_LABELS: Array<[string, string]> = [
  ["endpoint_window_ms", "Endpoint window"],
  ["stt_ms", "STT final"],
  ["llm_ttft_ms", "LLM TTFT"],
  ["first_speakable_ms", "First speakable"],
  ["tts_connection_wait_ms", "TTS connection wait"],
  ["tts_ttfb_ms", "TTS TTFB"],
  ["transport_playback_ms", "Transport + playback"],
];

function LatencyWaterfall({ metric }: { metric: TurnMetricsEvent }) {
  const numericStages = STAGE_LABELS.map(([key]) => metric.stages[key] ?? 0);
  const maxStage = Math.max(...numericStages, 1);
  const e2e = metric.stages.e2e_voice_to_voice_ms;
  return (
    <article>
      <h3>
        Turn {metric.turn_id.split(":").at(-1)} - {metric.endpointing_strategy}
      </h3>
      {STAGE_LABELS.map(([key, label]) => {
        const value = metric.stages[key];
        const width = value == null ? 0 : Math.max(2, (value / maxStage) * 100);
        return (
          <div key={key}>
            <span>{label}: </span>
            <span
              aria-label={`${label} bar`}
              style={{ display: "inline-block", width: `${width}%`, background: "#888" }}
            >
              &nbsp;
            </span>{" "}
            <span>{value == null ? "n/a" : `${value.toFixed(1)} ms`}</span>
          </div>
        );
      })}
      <strong>E2E: {e2e == null ? "n/a" : `${e2e.toFixed(1)} ms`}</strong>
      {metric.dimensions.interrupted === true && (
        <p>
          Status: Interrupted - acknowledgement proxy {String(
            metric.derived.barge_in_stop_ack_ms?.toFixed(1) ?? "n/a",
          )} ms
        </p>
      )}
      <p>Gated silent frames: {String(metric.dimensions.gated_silent_frames ?? "n/a")}</p>
    </article>
  );
}

export function App() {
  const [email, setEmail] = useState("");
  const [otp, setOtp] = useState("");
  const [otpRequested, setOtpRequested] = useState(false);
  const [token, setToken] = useState(() => localStorage.getItem("talkstream_token") ?? "");
  const [callActive, setCallActive] = useState(false);
  const [agentState, setAgentState] = useState("idle");
  const [transcript, setTranscript] = useState("");
  const [agentText, setAgentText] = useState("");
  const [error, setError] = useState("");
  const [metrics, setMetrics] = useState<TurnMetricsEvent[]>([]);
  const [interruptions, setInterruptions] = useState<string[]>([]);
  const [todos, setTodos] = useState<Todo[]>([]);
  const [balancePaise, setBalancePaise] = useState<number | null>(null);
  const [sessionCostPaise, setSessionCostPaise] = useState(0);
  const [billingWarning, setBillingWarning] = useState("");
  const clientRef = useRef<VoiceClient | null>(null);
  const identityRef = useRef<{ token: string; controller: AbortController } | null>(null);

  useEffect(
    () => () => {
      void clientRef.current?.stop();
    },
    [],
  );

  useLayoutEffect(() => {
    const scope = { token, controller: new AbortController() };
    identityRef.current = scope;
    const live = () => identityRef.current === scope && !scope.controller.signal.aborted;
    setTodos([]);
    if (token) void getCurrentUser(token).then(user => loadTodos(user.id)).then(todos => {
      if (live()) setTodos(todos);
    }).catch(error => {
      if (!live()) return;
      if (isAuthenticationExpiredError(error)) expireAuthentication();
      else setError("Could not load browser todos. Please try again.");
    });
    if (token) void getWallet(token)
      .then((wallet) => { if (live()) setBalancePaise(wallet.balance_paise); })
      .catch((walletError: unknown) => {
        if (!live()) return;
        if (isAuthenticationExpiredError(walletError)) expireAuthentication();
        else setError(String(walletError));
      });
    return () => {
      scope.controller.abort();
      const client = clientRef.current;
      clientRef.current = null;
      void client?.stop();
    };
  }, [token]);

  async function handleRequestOtp(event: FormEvent) {
    event.preventDefault();
    setError("");
    try {
      await requestOtp(email);
      setOtpRequested(true);
    } catch (requestError) {
      setError(String(requestError));
    }
  }

  async function handleVerifyOtp(event: FormEvent) {
    event.preventDefault();
    setError("");
    try {
      const accessToken = await verifyOtp(email, otp);
      localStorage.setItem("talkstream_token", accessToken);
      setToken(accessToken);
    } catch (verifyError) {
      setError(String(verifyError));
    }
  }

  function applyBilling(billing: BillingEvent) {
    setBalancePaise(billing.balance_paise);
    setSessionCostPaise(billing.session_cost_paise);
    setBillingWarning(
      billing.terminated_reason === "balance_exhausted"
        ? "Balance exhausted - call terminated"
        : billing.low_balance ? "Low balance" : "",
    );
  }

  async function startCall() {
    const scope = identityRef.current;
    if (!token || scope?.token !== token || scope.controller.signal.aborted) return;
    const oldClient = clientRef.current;
    clientRef.current = null;
    void oldClient?.stop();
    setError("");
    setTranscript("");
    setAgentText("");
    setMetrics([]);
    setInterruptions([]);
    setSessionCostPaise(0);
    setBillingWarning("");
    const live = () => identityRef.current === scope && !scope.controller.signal.aborted && clientRef.current === client;
    const client = new VoiceClient(token, {
      onState: (state) => {
        setAgentState(state);
        if (state.startsWith("ended") || state === "disconnected") setCallActive(false);
      },
      onTranscript: setTranscript,
      onAgentText: (text) => setAgentText((current) => `${current} ${text}`.trim()),
      onMetrics: (metric) => setMetrics((current) => [metric, ...current].slice(0, 8)),
      onInterrupted: (message) =>
        setInterruptions((current) => [message, ...current].slice(0, 8)),
      onTodosChanged: todos => { if (live()) setTodos(todos); },
      onBilling: applyBilling,
      onError: setError,
      onAuthenticationExpired: expireAuthentication,
    });
    clientRef.current = client;
    try {
      await client.start();
      if (live()) setCallActive(true);
    } catch (callError) {
      await client.stop();
      if (!live()) return;
      clientRef.current = null;
      if (isAuthenticationExpiredError(callError)) expireAuthentication();
      else setError(String(callError));
    }
  }

  async function stopCall() {
    const client = clientRef.current;
    const billing = await client?.stop();
    if (clientRef.current !== client) return;
    if (billing) applyBilling(billing);
    clientRef.current = null;
    setCallActive(false);
    setAgentState("idle");
  }

  function retireIdentity() {
    identityRef.current?.controller.abort();
    const client = clientRef.current;
    clientRef.current = null;
    void client?.stop();
    setCallActive(false);
    setAgentState("idle");
    setTodos([]);
    setBalancePaise(null);
    setSessionCostPaise(0);
    setTranscript("");
    setAgentText("");
    setMetrics([]);
    setInterruptions([]);
  }

  function logout() {
    retireIdentity();
    localStorage.removeItem("talkstream_token");
    setToken("");
  }

  function expireAuthentication() {
    retireIdentity();
    localStorage.removeItem("talkstream_token");
    setToken("");
    setCallActive(false);
    setAgentState("idle");
    setError(AUTHENTICATION_EXPIRED_MESSAGE);
  }

  async function mockRecharge() {
    setError("");
    try {
      const result = await rechargeWallet(token, 1000);
      setBalancePaise(result.balance_paise);
      setBillingWarning("");
    } catch (rechargeError) {
      if (isAuthenticationExpiredError(rechargeError)) expireAuthentication();
      else setError(String(rechargeError));
    }
  }

  if (!token) {
    return (
      <main>
        <h1>Talkstream Phase 4</h1>
        <form onSubmit={handleRequestOtp}>
          <label>
            Email
            <input
              type="email"
              required
              value={email}
              onChange={(event) => setEmail(event.target.value)}
            />
          </label>
          <button type="submit">Request OTP</button>
        </form>
        {otpRequested && (
          <form onSubmit={handleVerifyOtp}>
            <label>
              Six-digit OTP
              <input
                inputMode="numeric"
                pattern="[0-9]{6}"
                required
                value={otp}
                onChange={(event) => setOtp(event.target.value)}
              />
            </label>
            <button type="submit">Verify OTP</button>
          </form>
        )}
        {error && <pre>{error}</pre>}
      </main>
    );
  }

  return (
    <main>
      <h1>Talkstream Phase 4</h1>
      <p>State: {agentState}</p>
      <p>Wallet balance: {balancePaise == null ? "loading" : `${balancePaise} paise`}</p>
      <p>Current session cost: {sessionCostPaise} paise</p>
      {billingWarning && <p>{billingWarning}</p>}
      <button type="button" onClick={() => void mockRecharge()}>
        Mock recharge 1000 paise
      </button>
      <button type="button" onClick={() => void (callActive ? stopCall() : startCall())}>
        {callActive ? "Stop call" : "Start call"}
      </button>
      <button type="button" disabled={callActive} onClick={logout}>
        Log out
      </button>
      <h2>You</h2>
      <p>{transcript || "No transcript yet."}</p>
      <h2>Agent</h2>
      <p>{agentText || "No response yet."}</p>
      <h2>Latency waterfall</h2>
      {metrics.length === 0 ? (
        <p>No completed turns yet.</p>
      ) : (
        metrics.map((metric) => <LatencyWaterfall key={metric.turn_id} metric={metric} />)
      )}
      <h2>Interruptions</h2>
      {interruptions.length === 0 ? (
        <p>No interruptions yet.</p>
      ) : (
        <ul>
          {interruptions.map((message, index) => (
            <li key={`${index}-${message}`}>{message}</li>
          ))}
        </ul>
      )}
      <h2>Todos</h2>
      {todos.length === 0 ? (
        <p>No todos yet.</p>
      ) : (
        <ul>
          {todos.map((todo) => (
            <li key={todo.id}>
              {todo.completed ? "Done: " : "Open: "}
              {todo.text}
            </li>
          ))}
        </ul>
      )}
      {error && <pre>{error}</pre>}
    </main>
  );
}

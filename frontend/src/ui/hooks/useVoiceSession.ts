import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";

import {
  executeTodoTool,
  loadTodos,
  type Todo,
  type TodoToolName,
} from "../../store/todos";
import { isAuthenticationExpiredError } from "../../api/errors";
import { getCurrentUser } from "../../api/auth";
import { turnIndexOf } from "../latency";
import { advanceSpeech, revealPending, upsertActionReceipts, type ActionReceipt } from "../conversation";
import { VoiceClient, type BillingEvent } from "../../voice/client";
import type { SearchSource, ServerEvent, TurnMetricsEvent } from "../../voice/protocol";

export type CallStatus = "idle" | "connecting" | "active" | "ended" | "denied";

export type AgentStateName =
  | "listening"
  | "user_speaking"
  | "thinking"
  | "speaking"
  | "interrupted";

export type TimedTurnMetrics = {
  metric: TurnMetricsEvent;
  atSeconds: number;
};

export type TranscriptEntry = {
  who: "user" | "agent" | "system";
  text: string;
  languageCode: string | null;
  atSeconds: number;
  interrupted: boolean;
  turnId?: string;
  /** Agent text heard so far; null or absent once the whole answer is shown. */
  spokenChars?: number | null;
  sources?: SearchSource[];
};

export type VoiceSessionState = {
  callStatus: CallStatus;
  endReason: string | null;
  agentState: AgentStateName;
  transportStatus: "transmitting_speech" | "silence_not_transmitting" | null;
  sessionId: string | null;
  elapsedSeconds: number;
  sessionCostPaise: number;
  balancePaise: number | null;
  lowBalance: boolean;
  userText: string;
  agentText: string;
  agentTurnInterrupted: boolean;
  languageCode: string | null;
  languageFlash: boolean;
  log: TranscriptEntry[];
  metrics: TimedTurnMetrics[];
  actionReceipts: ActionReceipt[];
  todos: Todo[];
  todoError: string;
  agentTodoToast: string;
  muted: boolean;
  error: string;
};

const INITIAL_STATE: VoiceSessionState = {
  callStatus: "idle",
  endReason: null,
  agentState: "listening",
  transportStatus: null,
  sessionId: null,
  elapsedSeconds: 0,
  sessionCostPaise: 0,
  balancePaise: null,
  lowBalance: false,
  userText: "",
  agentText: "",
  agentTurnInterrupted: false,
  languageCode: null,
  languageFlash: false,
  log: [],
  metrics: [],
  actionReceipts: [],
  todos: [],
  todoError: "",
  agentTodoToast: "",
  muted: false,
  error: "",
};

type TodoIdentityScope = {
  token: string;
  userId: string | null;
  lookup: Promise<string> | null;
  controller: AbortController;
};

async function todoUserId(scope: TodoIdentityScope): Promise<string> {
  scope.controller.signal.throwIfAborted();
  if (scope.userId) return scope.userId;
  scope.lookup ??= getCurrentUser(scope.token).then(user => {
    scope.controller.signal.throwIfAborted();
    scope.userId = user.id;
    return user.id;
  }).finally(() => { scope.lookup = null; });
  return scope.lookup;
}

function isPermissionError(message: string): boolean {
  return /notallowed|permission|denied/i.test(message);
}

export function useVoiceSession(
  token: string,
  onBalanceChange?: (balancePaise: number) => void,
  onAuthenticationExpired?: () => void,
) {
  const [state, setState] = useState<VoiceSessionState>(INITIAL_STATE);
  const clientRef = useRef<VoiceClient | null>(null);
  // Incremented on every start attempt and on teardown. Awaits inside start()
  // re-check it so overlapping attempts (React StrictMode double effects,
  // rapid retries) collapse to exactly one live client and one session.
  const attemptRef = useRef(0);
  const cleanupRef = useRef<Promise<void>>(Promise.resolve());
  const elapsedRef = useRef(0);
  const todosBeforeRef = useRef<Todo[]>([]);
  const identityRef = useRef<TodoIdentityScope | null>(null);
  const toastTimerRef = useRef<number | null>(null);
  const flashTimerRef = useRef<number | null>(null);

  useEffect(() => {
    return () => {
      attemptRef.current += 1;
      const client = clientRef.current;
      clientRef.current = null;
      void client?.stop();
      if (toastTimerRef.current != null) window.clearTimeout(toastTimerRef.current);
      if (flashTimerRef.current != null) window.clearTimeout(flashTimerRef.current);
    };
  }, []);

  useEffect(() => {
    if (state.callStatus !== "active") return;
    const interval = window.setInterval(() => {
      elapsedRef.current += 1;
      setState((current) => ({ ...current, elapsedSeconds: elapsedRef.current }));
    }, 1000);
    return () => window.clearInterval(interval);
  }, [state.callStatus]);

  // Agent text arrives before its audio; reveal it as the audio actually plays.
  useEffect(() => {
    if (state.callStatus !== "active") {
      setState((current) => {
        const log = revealPending(current.log);
        return log === current.log ? current : { ...current, log };
      });
      return;
    }
    const interval = window.setInterval(() => {
      const client = clientRef.current;
      if (!client) return;
      setState((current) => {
        const log = advanceSpeech(current.log, (turnId) => client.speechProgress(turnId));
        return log === current.log ? current : { ...current, log };
      });
    }, 80);
    return () => window.clearInterval(interval);
  }, [state.callStatus]);

  const showAgentTodoToast = useCallback((message: string) => {
    setState((current) => ({ ...current, agentTodoToast: message }));
    if (toastTimerRef.current != null) window.clearTimeout(toastTimerRef.current);
    toastTimerRef.current = window.setTimeout(() => {
      setState((current) => ({ ...current, agentTodoToast: "" }));
    }, 3600);
  }, []);

  const retireClient = useCallback((client: VoiceClient) => {
    if (clientRef.current === client) clientRef.current = null;
    const cleanup = client.stop().catch(() => null);
    cleanupRef.current = Promise.allSettled([cleanupRef.current, cleanup]).then(() => undefined);
    return cleanup;
  }, []);

  const applyBilling = useCallback((billing: BillingEvent) => {
    elapsedRef.current = Math.max(elapsedRef.current, billing.seconds);
    onBalanceChange?.(billing.balance_paise);
    setState((current) => ({
      ...current,
      elapsedSeconds: elapsedRef.current,
      sessionCostPaise: billing.session_cost_paise,
      balancePaise: billing.balance_paise,
      lowBalance: billing.low_balance,
      endReason: billing.terminated_reason ?? current.endReason,
    }));
  }, [onBalanceChange]);

  useLayoutEffect(() => {
    const scope: TodoIdentityScope = { token, userId: null, lookup: null, controller: new AbortController() };
    identityRef.current = scope;
    const live = () => identityRef.current === scope && !scope.controller.signal.aborted;
    attemptRef.current += 1;
    const client = clientRef.current;
    clientRef.current = null;
    if (client) retireClient(client);
    todosBeforeRef.current = [];
    elapsedRef.current = 0;
    if (toastTimerRef.current != null) window.clearTimeout(toastTimerRef.current);
    if (flashTimerRef.current != null) window.clearTimeout(flashTimerRef.current);
    setState({ ...INITIAL_STATE });
    if (token) void (async () => {
      try {
        const userId = await todoUserId(scope);
        if (!live()) return;
        const todos = await loadTodos(userId);
        if (!live()) return;
        todosBeforeRef.current = todos;
        setState(current => ({ ...current, todos }));
      } catch (error) {
        if (!live()) return;
        if (isAuthenticationExpiredError(error)) {
          scope.controller.abort();
          const currentClient = clientRef.current;
          if (currentClient) retireClient(currentClient);
          onAuthenticationExpired?.();
        } else setState(current => ({ ...current, todoError: "Could not load browser todos. Please try again." }));
      }
    })();
    return () => {
      scope.controller.abort();
      if (identityRef.current === scope) identityRef.current = null;
      attemptRef.current += 1;
      const currentClient = clientRef.current;
      if (currentClient) retireClient(currentClient);
    };
  }, [token, retireClient, onAuthenticationExpired]);

  const start = useCallback(async () => {
    if (!token || clientRef.current) return;
    const attempt = ++attemptRef.current;
    await cleanupRef.current;
    if (attempt !== attemptRef.current || clientRef.current) return;
    elapsedRef.current = 0;
    setState((current) => ({
      ...INITIAL_STATE,
      todos: current.todos,
      balancePaise: current.balancePaise,
      callStatus: "connecting",
    }));

    // Resolve the mic permission BEFORE opening the WebSocket: the server
    // starts billing the moment the session socket connects, so the call
    // must not connect while the permission prompt is still pending.
    try {
      const probe = await navigator.mediaDevices.getUserMedia({ audio: true });
      probe.getTracks().forEach((track) => track.stop());
    } catch (error) {
      if (attempt !== attemptRef.current) return;
      if (isAuthenticationExpiredError(error)) {
        onAuthenticationExpired?.();
        return;
      }
      const message = String(error);
      setState((current) => ({
        ...current,
        callStatus: isPermissionError(message) ? "denied" : "ended",
        endReason: isPermissionError(message) ? null : "error",
        error: isPermissionError(message) ? "" : message,
      }));
      return;
    }
    if (attempt !== attemptRef.current || clientRef.current) return;

    // Ignore events from any client that is no longer the tracked one, so a
    // superseded session can never fight the live one over UI state.
    let self: VoiceClient | null = null;
    const live = () => self != null && clientRef.current === self && identityRef.current?.token === token && !identityRef.current.controller.signal.aborted;

    const client = new VoiceClient(token, {
      onState: (value) => {
        if (!live()) return;
        if (value === "disconnected" || value.startsWith("ended")) {
          setState((current) => ({
            ...current,
            callStatus: "ended",
            endReason:
              current.endReason ??
              (value.includes("balance") ? "balance_exhausted" : "disconnected"),
          }));
          if (self != null) retireClient(self);
        }
      },
      onReady: (event) => {
        if (!live()) return;
        setState((current) => ({
          ...current,
          callStatus: "active",
          sessionId: event.session_id,
          balancePaise: event.balance_paise,
        }));
      },
      onAgentState: (event: Extract<ServerEvent, { type: "agent_state" }>) => {
        if (!live()) return;
        setState((current) => ({
          ...current,
          agentState: event.state,
          transportStatus: event.transport_status ?? current.transportStatus,
          agentTurnInterrupted:
            event.state === "interrupted" ? true : current.agentTurnInterrupted,
          log:
            event.state === "interrupted"
              ? markLastAgentInterrupted(current.log)
              : current.log,
        }));
      },
      onTranscript: (text, languageCode, turnId) => {
        if (!live()) return;
        const lang = languageCode ?? null;
        setState((current) => {
          const flash = lang != null && lang !== current.languageCode;
          if (flash) {
            if (flashTimerRef.current != null) window.clearTimeout(flashTimerRef.current);
            flashTimerRef.current = window.setTimeout(() => {
              setState((inner) => ({ ...inner, languageFlash: false }));
            }, 700);
          }
          return {
            ...current,
            userText: text,
            agentText: "",
            agentTurnInterrupted: false,
            languageCode: lang,
            languageFlash: flash || current.languageFlash,
            log: [
              ...revealPending(current.log),
              {
                who: "user" as const,
                text,
                languageCode: lang,
                atSeconds: elapsedRef.current,
                interrupted: false,
                turnId,
              },
            ],
          };
        });
      },
      onAgentText: (text, turnId) => {
        if (!live()) return;
        setState((current) => ({
          ...current,
          agentText: current.agentText ? `${current.agentText} ${text}` : text,
          log: upsertAgentTurn(current.log, turnId, elapsedRef.current, (entry) => ({
            ...entry,
            text: entry.text ? `${entry.text} ${text}` : text,
          })),
        }));
      },
      onSearchResults: (results, turnId) => {
        if (!live()) return;
        setState((current) => ({
          ...current,
          log: upsertAgentTurn(current.log, turnId, elapsedRef.current, (entry) => ({
            ...entry,
            sources: [...(entry.sources ?? []), ...results],
          })),
        }));
      },
      onMetrics: (metric) => {
        if (!live()) return;
        setState((current) => {
          // The server re-sends turn_metrics for a turn once barge-in
          // timings are known, so upsert by turn_id instead of appending.
          const existing = current.metrics.find(
            (entry) => entry.metric.turn_id === metric.turn_id,
          );
          const merged = [
            { metric, atSeconds: existing?.atSeconds ?? elapsedRef.current },
            ...current.metrics.filter(
              (entry) => entry.metric.turn_id !== metric.turn_id,
            ),
          ]
            .sort(
              (a, b) => turnIndexOf(b.metric.turn_id) - turnIndexOf(a.metric.turn_id),
            )
            .slice(0, 24);
          return {
            ...current,
            metrics: merged,
            actionReceipts: upsertActionReceipts(current.actionReceipts, metric, elapsedRef.current),
          };
        });
      },
      onInterrupted: (message) => {
        if (!live()) return;
        setState((current) => ({
          ...current,
          log: [
            ...current.log,
            {
              who: "system" as const,
              text: message,
              languageCode: null,
              atSeconds: elapsedRef.current,
              interrupted: false,
            },
          ],
        }));
      },
      onTodosChanged: (todos) => {
        if (!live()) return;
        const before = todosBeforeRef.current;
        todosBeforeRef.current = todos;
        const added = todos.find((todo) => !before.some((item) => item.id === todo.id));
        if (added) showAgentTodoToast(`Agent added: ${added.text}`);
        else if (todos.length < before.length) showAgentTodoToast("Agent updated your todos");
        else if (todos.some((todo) => todo.completed !== before.find((item) => item.id === todo.id)?.completed)) {
          showAgentTodoToast("Agent completed a todo");
        }
        setState((current) => ({ ...current, todos }));
      },
      onBilling: (billing) => {
        if (!live()) return;
        applyBilling(billing);
      },
      onCallEnded: (event) => {
        if (!live()) return;
        setState((current) => ({
          ...current,
          callStatus: "ended",
          endReason: event.reason,
        }));
      },
      onError: (message) => {
        if (!live()) return;
        setState((current) => ({ ...current, error: message }));
      },
      onAuthenticationExpired: () => {
        if (live()) onAuthenticationExpired?.();
      },
    });
    self = client;

    clientRef.current = client;
    try {
      await client.start();
    } catch (error) {
      if (clientRef.current === client) clientRef.current = null;
      await client.stop().catch(() => undefined);
      if (attempt !== attemptRef.current) return;
      const message = String(error);
      setState((current) => ({
        ...current,
        callStatus: isPermissionError(message) ? "denied" : "ended",
        endReason: isPermissionError(message) ? null : "error",
        error: isPermissionError(message) ? "" : message,
      }));
    }
  }, [token, applyBilling, onAuthenticationExpired, retireClient, showAgentTodoToast]);

  const stop = useCallback(async ({ discardFinalBilling = false }: { discardFinalBilling?: boolean } = {}) => {
    const attempt = ++attemptRef.current;
    if (discardFinalBilling) {
      identityRef.current?.controller.abort();
      todosBeforeRef.current = [];
      setState({ ...INITIAL_STATE });
    }
    const client = clientRef.current;
    clientRef.current = null;
    const finalBilling = client ? await retireClient(client) : null;
    await cleanupRef.current;
    if (attempt !== attemptRef.current) return;
    if (!discardFinalBilling && finalBilling) applyBilling(finalBilling);
    setState((current) => ({
      ...current,
      callStatus: current.callStatus === "active" || current.callStatus === "connecting"
        ? "ended"
        : current.callStatus,
      endReason: current.endReason ?? "user",
      muted: false,
    }));
  }, [retireClient, applyBilling]);

  const setMuted = useCallback((muted: boolean) => {
    clientRef.current?.setMuted(muted);
    setState((current) => ({ ...current, muted }));
  }, []);

  const runTodoTool = useCallback(
    async (name: TodoToolName, args: Record<string, unknown>): Promise<boolean> => {
      const scope = identityRef.current;
      const live = () => scope !== null && scope.token === token && identityRef.current === scope && !scope.controller.signal.aborted;
      if (!token || !live() || !scope) return false;
      let committed = false;
      try {
        const userId = await todoUserId(scope);
        if (!live()) return false;
        const result = await executeTodoTool({ userId, callId: "manual_" + crypto.randomUUID(),
          name, arguments: args, signal: scope.controller.signal });
        if (!live()) return false;
        if (!result.ok) {
          setState(current => ({ ...current, todoError: result.error }));
          return false;
        }
        committed = true;
        const todos = await loadTodos(userId);
        if (!live()) return false;
        todosBeforeRef.current = todos;
        setState(current => ({ ...current, todos, todoError: "" }));
        return true;
      } catch (error) {
        if (live() && isAuthenticationExpiredError(error)) {
          scope.controller.abort();
          const client = clientRef.current;
          if (client) retireClient(client);
          onAuthenticationExpired?.();
          return false;
        }
        if (live()) setState(current => ({ ...current, todoError: committed
          ? "Todo saved, but the list could not be refreshed." : "Could not save browser todos. Please try again." }));
        return live() && committed;
      }
    },
    [token, retireClient, onAuthenticationExpired],
  );

  const reset = useCallback(async () => {
    const attempt = ++attemptRef.current;
    const client = clientRef.current;
    if (client) retireClient(client);
    await cleanupRef.current;
    if (attempt !== attemptRef.current) return;
    elapsedRef.current = 0;
    setState((current) => ({
      ...INITIAL_STATE,
      todos: current.todos,
      balancePaise: current.balancePaise,
    }));
  }, [retireClient]);

  const visibleState = identityRef.current?.token === token && !identityRef.current.controller.signal.aborted ? state : { ...INITIAL_STATE };
  return { state: visibleState, start, stop, reset, setMuted, runTodoTool };
}

function upsertAgentTurn(
  log: TranscriptEntry[],
  turnId: string,
  atSeconds: number,
  update: (entry: TranscriptEntry) => TranscriptEntry,
): TranscriptEntry[] {
  const index = log.map((entry) => entry.who === "agent" && entry.turnId === turnId).lastIndexOf(true);
  if (index >= 0) return log.map((entry, i) => (i === index ? update(entry) : entry));
  return [...log, update({ who: "agent", text: "", languageCode: null, atSeconds, interrupted: false, turnId, spokenChars: 0 })];
}

function markLastAgentInterrupted(log: TranscriptEntry[]): TranscriptEntry[] {
  const lastAgentIndex = log.map((entry) => entry.who).lastIndexOf("agent");
  if (lastAgentIndex < 0) return log;
  return log.map((entry, index) =>
    index === lastAgentIndex ? { ...entry, interrupted: true } : entry,
  );
}

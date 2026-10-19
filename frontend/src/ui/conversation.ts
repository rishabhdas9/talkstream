import type { TranscriptEntry } from "./hooks/useVoiceSession";
import type { TurnMetricsEvent } from "../voice/protocol";

export type ActionReceipt = {
  callId: string;
  turnId: string;
  name: string;
  status: "confirmed" | "failed" | "unconfirmed";
  atSeconds: number;
};

export type ConversationItem =
  | { kind: "turn"; turn: TranscriptEntry }
  | { kind: "receipt"; receipt: ActionReceipt };

const ACTION_NAMES: Record<string, [string, string]> = {
  todo_add: ["Add todo", "Todo added"],
  todo_list: ["Read todos", "Todos read"],
  todo_update: ["Update todo", "Todo updated"],
  todo_complete: ["Complete todo", "Todo completed"],
  todo_delete: ["Delete todo", "Todo deleted"],
  web_search: ["Web search", "Web search completed"],
};

export function groupTranscript(log: TranscriptEntry[]): TranscriptEntry[] {
  return log.reduce<TranscriptEntry[]>((turns, entry) => {
    const previous = turns.at(-1);
    if (entry.who === "agent" && previous?.who === "agent" && previous.turnId === entry.turnId) {
      previous.text = `${previous.text} ${entry.text}`;
      previous.interrupted ||= entry.interrupted;
    } else {
      turns.push({ ...entry });
    }
    return turns;
  }, []);
}

export function upsertActionReceipts(
  current: ActionReceipt[],
  metric: TurnMetricsEvent,
  atSeconds: number,
): ActionReceipt[] {
  const receipts = [...current];
  for (const span of metric.tool_spans) {
    if (typeof span.call_id !== "string" || !span.call_id || typeof span.name !== "string") continue;
    const existingIndex = receipts.findIndex((receipt) => receipt.callId === span.call_id);
    const receipt: ActionReceipt = {
      callId: span.call_id,
      turnId: metric.turn_id,
      name: span.name,
      status: span.outcome === "success" ? "confirmed" : span.outcome === "error" ? "failed" : "unconfirmed",
      atSeconds: existingIndex < 0 ? atSeconds : receipts[existingIndex].atSeconds,
    };
    if (existingIndex < 0) receipts.push(receipt);
    else receipts[existingIndex] = receipt;
  }
  return receipts;
}

export function actionReceiptText(receipt: ActionReceipt): string {
  const [action, success] = ACTION_NAMES[receipt.name] ?? ["Tool action", "Tool action completed"];
  if (receipt.status === "confirmed") return success;
  return action;
}

export function callRecap(log: TranscriptEntry[], receipts: ActionReceipt[]) {
  return {
    spokenTurns: log.filter((entry) => entry.who === "user").length,
    confirmedActions: receipts.filter((receipt) => receipt.status === "confirmed").length,
    failedActions: receipts.filter((receipt) => receipt.status === "failed").length,
    unconfirmedActions: receipts.filter((receipt) => receipt.status === "unconfirmed").length,
  };
}

export function conversationTimeline(log: TranscriptEntry[], receipts: ActionReceipt[]): ConversationItem[] {
  const turns = groupTranscript(log);
  const lastTurnIndex = new Map<string, number>();
  turns.forEach((turn, index) => {
    if (turn.turnId) lastTurnIndex.set(turn.turnId, index);
  });
  const receiptsByTurn = new Map<number, ActionReceipt[]>();
  const unanchored: ActionReceipt[] = [];
  for (const receipt of receipts) {
    const index = lastTurnIndex.get(receipt.turnId);
    if (index === undefined) unanchored.push(receipt);
    else receiptsByTurn.set(index, [...(receiptsByTurn.get(index) ?? []), receipt]);
  }
  return [
    ...turns.flatMap((turn, index): ConversationItem[] => [
      { kind: "turn", turn },
      ...(receiptsByTurn.get(index) ?? []).map((receipt): ConversationItem => ({ kind: "receipt", receipt })),
    ]),
    ...unanchored.map((receipt): ConversationItem => ({ kind: "receipt", receipt })),
  ];
}

export type SpeechProgress = { playedMs: number; totalMs: number; finished: boolean };

// Until all answer audio has arrived, assume roughly this speaking rate.
const ESTIMATED_MS_PER_CHAR = 60;

/** Advances each pending agent turn to the share of its text already heard; returns `log` itself when nothing moved. */
export function advanceSpeech(log: TranscriptEntry[], progress: (turnId: string) => SpeechProgress): TranscriptEntry[] {
  let changed = false;
  const next = log.map((entry) => {
    if (entry.who !== "agent" || entry.spokenChars == null || !entry.turnId) return entry;
    const { playedMs, totalMs, finished } = progress(entry.turnId);
    const length = entry.text.length;
    const spoken = finished && !entry.interrupted
      ? null
      : Math.max(entry.spokenChars, Math.min(length, Math.floor((length * playedMs) / Math.max(totalMs, length * ESTIMATED_MS_PER_CHAR, 1))));
    if (spoken === entry.spokenChars) return entry;
    changed = true;
    return { ...entry, spokenChars: spoken };
  });
  return changed ? next : log;
}

/** Shows the full text of finished turns; an interrupted turn keeps only what was heard. */
export function revealPending(log: TranscriptEntry[]): TranscriptEntry[] {
  if (!log.some((entry) => entry.spokenChars != null && !entry.interrupted)) return log;
  return log.map((entry) => (entry.spokenChars != null && !entry.interrupted ? { ...entry, spokenChars: null } : entry));
}

/** The heard part of a turn, cut back to a whole word. */
export function spokenText(entry: TranscriptEntry): string {
  if (entry.spokenChars == null || entry.spokenChars >= entry.text.length) return entry.text;
  const cut = entry.text.lastIndexOf(" ", entry.spokenChars);
  return cut > 0 ? entry.text.slice(0, cut) : "";
}

/** Splits agent text into words, turning the model's `**bold**` markers into a flag. */
export function richWords(text: string): { word: string; bold: boolean }[] {
  let bold = false;
  return text.split(" ").map((raw) => {
    const parts = raw.split("**");
    const startsBold = bold;
    if (parts.length % 2 === 0) bold = !bold;
    return { word: parts.join(""), bold: startsBold || parts.length > 1 };
  });
}

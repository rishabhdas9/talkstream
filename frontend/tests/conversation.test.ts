import assert from "node:assert/strict";
import test from "node:test";

import { actionReceiptText, advanceSpeech, callRecap, conversationTimeline, groupTranscript, revealPending, richWords, spokenText, upsertActionReceipts } from "../src/ui/conversation.ts";
import type { TranscriptEntry } from "../src/ui/hooks/useVoiceSession.ts";
import type { TurnMetricsEvent } from "../src/voice/protocol.ts";

function entry(who: TranscriptEntry["who"], text: string, interrupted = false): TranscriptEntry {
  return { who, text, languageCode: null, atSeconds: 3, interrupted };
}

test("streamed assistant chunks form one readable conversation turn", () => {
  const log = [entry("user", "नमस्ते"), entry("agent", "Hello"), entry("agent", "there", true), entry("system", "Interrupted"), entry("user", "Thanks")];
  assert.deepEqual(groupTranscript(log).map(({ who, text, interrupted }) => ({ who, text, interrupted })), [
    { who: "user", text: "नमस्ते", interrupted: false },
    { who: "agent", text: "Hello there", interrupted: true },
    { who: "system", text: "Interrupted", interrupted: false },
    { who: "user", text: "Thanks", interrupted: false },
  ]);
  assert.equal(log[1].text, "Hello");
});

test("assistant chunks from different turns stay separate", () => {
  const log = [
    { ...entry("agent", "First answer"), turnId: "turn:1" },
    { ...entry("agent", "Second answer"), turnId: "turn:2" },
  ];
  assert.equal(groupTranscript(log).length, 2);
});

function metric(
  turnId: string,
  toolSpans: TurnMetricsEvent["tool_spans"],
): TurnMetricsEvent {
  return {
    type: "turn_metrics",
    turn_id: turnId,
    endpointing_strategy: "local_vad",
    stages: {},
    derived: {},
    dimensions: {},
    tool_spans: toolSpans,
    missing_timestamps: [],
  };
}

test("tool receipts use measured outcomes and deduplicate metric replays", () => {
  const initial = upsertActionReceipts([], metric("turn:1", [
    { call_id: "add-1", name: "todo_add", outcome: "unknown", duration_ms: 900 },
    { call_id: "search-1", name: "web_search", outcome: "error", duration_ms: 200 },
  ]), 4);
  const updated = upsertActionReceipts(initial, metric("turn:1", [
    { call_id: "add-1", name: "todo_add", outcome: "success", duration_ms: 900 },
    { call_id: "search-1", name: "web_search", outcome: "error", duration_ms: 200 },
  ]), 7);
  assert.equal(updated.length, 2);
  assert.deepEqual(updated.map(({ status, atSeconds }) => ({ status, atSeconds })), [
    { status: "confirmed", atSeconds: 4 },
    { status: "failed", atSeconds: 4 },
  ]);
  assert.equal(actionReceiptText(updated[0]), "Todo added");
  assert.equal(actionReceiptText(updated[1]), "Web search");
  assert.equal(initial[0].status, "unconfirmed");
});

test("cancelled and unfamiliar outcomes never become confirmed actions", () => {
  const receipts = upsertActionReceipts([], metric("turn:2", [
    { call_id: "delete-1", name: "todo_delete", outcome: "cancelled", duration_ms: 10 },
    { call_id: "future-1", name: "future_tool", outcome: "unexpected", duration_ms: 10 },
  ]), 8);
  assert.deepEqual(receipts.map(({ status }) => status), ["unconfirmed", "unconfirmed"]);
  assert.equal(actionReceiptText(receipts[0]), "Delete todo");
  assert.equal(actionReceiptText(receipts[1]), "Tool action");
});

test("call recap counts observed outcomes, not assistant claims", () => {
  const log = [entry("user", "Add milk"), entry("agent", "I added milk"), entry("user", "Delete it")];
  const receipts = upsertActionReceipts([], metric("turn:1", [
    { call_id: "add-1", name: "todo_add", outcome: "success", duration_ms: 100 },
    { call_id: "delete-1", name: "todo_delete", outcome: "unknown", duration_ms: 100 },
  ]), 4);
  assert.deepEqual(callRecap(log, receipts), {
    spokenTurns: 2,
    confirmedActions: 1,
    failedActions: 0,
    unconfirmedActions: 1,
  });
  assert.equal(callRecap([entry("agent", "I did it")], []).confirmedActions, 0);
});

test("tool outcome appears after the reply for its own turn", () => {
  const log = [
    { ...entry("user", "Add milk"), turnId: "turn:1" },
    { ...entry("agent", "Adding it"), turnId: "turn:1" },
    { ...entry("user", "Thanks"), turnId: "turn:2" },
  ];
  const receipts = upsertActionReceipts([], metric("turn:1", [
    { call_id: "add-1", name: "todo_add", outcome: "success", duration_ms: 100 },
  ]), 5);
  assert.deepEqual(conversationTimeline(log, receipts).map((item) =>
    item.kind === "turn" ? item.turn.text : item.receipt.callId,
  ), ["Add milk", "Adding it", "add-1", "Thanks"]);
  assert.equal(conversationTimeline([], receipts)[0].kind, "receipt");
});

test("agent text follows playback instead of arriving all at once", () => {
  const pending = { ...entry("agent", "Two cafes are open near you."), turnId: "t1", spokenChars: 0 };
  const total = pending.text.length * 60;
  const halfway = advanceSpeech([pending], () => ({ playedMs: total / 2, totalMs: total, finished: false }));
  assert.equal(spokenText(halfway[0]), "Two cafes are");
  const silent = advanceSpeech(halfway, () => ({ playedMs: 0, totalMs: total, finished: false }));
  assert.equal(silent, halfway, "progress never moves backwards and unchanged logs keep identity");
  const done = advanceSpeech(halfway, () => ({ playedMs: total, totalMs: total, finished: true }));
  assert.equal(spokenText(done[0]), pending.text);
});

test("an interrupted answer keeps only the words that were heard", () => {
  const cut = { ...entry("agent", "Sure, the first one is Blue Tokai.", true), turnId: "t1", spokenChars: 12 };
  assert.equal(advanceSpeech([cut], () => ({ playedMs: 0, totalMs: 1, finished: true }))[0].spokenChars, 12);
  assert.equal(spokenText(revealPending([cut])[0]), "Sure, the");
  const unheard = { ...entry("agent", "Unplayed answer"), turnId: "t2", spokenChars: 0 };
  assert.equal(spokenText(revealPending([unheard])[0]), "Unplayed answer");
});

test("markdown bold from the model renders as emphasis, not asterisks", () => {
  const words = richWords("1. **Waffle Magic** — open, **Chelvies**.");
  assert.deepEqual(words.map((w) => w.word).join(" "), "1. Waffle Magic — open, Chelvies.");
  assert.deepEqual(words.filter((w) => w.bold).map((w) => w.word), ["Waffle", "Magic", "Chelvies."]);
  assert.deepEqual(richWords("**Half open").map((w) => w.bold), [true, true], "an unclosed marker mid-stream still reads as bold");
});

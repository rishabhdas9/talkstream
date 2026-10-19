import assert from "node:assert/strict";
import test from "node:test";

import { FeedbackCueLifecycle } from "../src/audio/feedback.ts";
import { parseServerEvent } from "../src/voice/protocol.ts";

test("answer start invalidates the active cue without changing answer scheduling", () => {
  const lifecycle = new FeedbackCueLifecycle();
  const generation = lifecycle.begin("turn-1", "cue-1");

  assert.equal(lifecycle.canStart("turn-1", "cue-1", generation), true);
  lifecycle.cancel("turn-1", "cue-1");
  assert.equal(lifecycle.canStart("turn-1", "cue-1", generation), false);
});

test("stale cue events cannot replace or cancel the current turn", () => {
  const lifecycle = new FeedbackCueLifecycle();
  const oldGeneration = lifecycle.begin("turn-1", "cue-1");
  const currentGeneration = lifecycle.begin("turn-2", "cue-2");

  lifecycle.cancel("turn-1", "cue-1");

  assert.equal(lifecycle.canStart("turn-1", "cue-1", oldGeneration), false);
  assert.equal(lifecycle.canStart("turn-2", "cue-2", currentGeneration), true);
});

test("response cues accept only bundled cue keys and finite timing anchors", () => {
  const valid = JSON.stringify({
    type: "response_cue",
    turn_id: "turn-1",
    generation_id: "g-1",
    cue_id: "cue-1",
    cue_key: "neutral_ack",
    language_code: "hi-IN",
    delay_ms: 350,
    last_speech_capture_time_ms: 1000,
  });

  assert.equal(parseServerEvent(valid).type, "response_cue");
  assert.throws(() => parseServerEvent(valid.replace("neutral_ack", "https://example.com/a")));
});

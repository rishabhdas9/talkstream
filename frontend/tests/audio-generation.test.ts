import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { PcmPlaybackQueue } from "../src/audio/playback.ts";

import { decodeOutputAudio, encodeCaptureFrame } from "../src/voice/protocol.ts";

const golden = JSON.parse(readFileSync(new URL("../../backend/tests/fixtures/output-audio-v2.json", import.meta.url), "utf8"));
const bytes = (hex: string): ArrayBuffer => Uint8Array.from(Buffer.from(hex, "hex")).buffer;

function frame(header: unknown, payload = new Uint8Array([0, 0])): ArrayBuffer {
  const json = new TextEncoder().encode(JSON.stringify(header));
  const raw = new Uint8Array(9 + json.length + payload.length);
  raw.set([84, 76, 75, 83, 2]);
  new DataView(raw.buffer).setUint32(5, json.length, false);
  raw.set(json, 9); raw.set(payload, 9 + json.length);
  return raw.buffer;
}

test("Python version2 golden frame decodes with its own immutable identity", () => {
  const result = decodeOutputAudio(bytes(golden.frame_hex));
  assert.deepEqual(result.header, golden.header);
  assert.equal(Buffer.from(result.payload).toString("hex"), golden.payload_hex);
});

test("capture uses the new application identity with the original v1 layout", () => {
  const raw = encodeCaptureFrame(7, 12.5, new ArrayBuffer(2));
  assert.equal(Buffer.from(raw).subarray(0, 5).toString("hex"), "544c4b4301");
  assert.equal(new DataView(raw).getBigUint64(5, false), 7n);
  assert.equal(new DataView(raw).getFloat64(13, false), 12.5);
});

test("rejects malformed framing and invalid UTF8 before audio decoding", () => {
  const valid = bytes(golden.frame_hex);
  for (const size of [0, 4, 8, 9, valid.byteLength - 1]) {
    assert.throws(() => decodeOutputAudio(valid.slice(0, size)));
  }
  for (const [offset, value] of [[0, 0], [4, 3]]) {
    const raw = valid.slice(0); new DataView(raw).setUint8(offset, value);
    assert.throws(() => decodeOutputAudio(raw));
  }
  for (const size of [0, 4097, 2**32 - 1]) {
    const raw = valid.slice(0); new DataView(raw).setUint32(5, size, false);
    assert.throws(() => decodeOutputAudio(raw));
  }
  const invalid = valid.slice(0); new Uint8Array(invalid)[9] = 255;
  assert.throws(() => decodeOutputAudio(invalid));
});

test("rejects unknown codec, invalid identities, sequence, rate and extra fields", () => {
  for (const [key, value] of [
    ["turn_id", ""], ["generation_id", null], ["turn_id", "x".repeat(129)],
    ["sequence", true], ["sequence", -1], ["sequence", 1.5], ["sequence", 2**32],
    ["codec", "wav"], ["codec", "mp3"], ["sample_rate_hz", 23999],
    ["sample_rate_hz", true], ["kind", "other"], ["extra", "unexpected"],
    ["kind", []], ["turn_id", "\ud800"],
  ]) assert.throws(() => decodeOutputAudio(frame({ ...golden.header, [key as string]: value })));
  assert.throws(() => decodeOutputAudio(frame(null)));
  assert.throws(() => decodeOutputAudio(frame({})));
  assert.throws(() => decodeOutputAudio(frame(golden.header, new Uint8Array())));
  assert.throws(() => decodeOutputAudio(frame(golden.header, new Uint8Array(256 * 1024 + 2))));
  assert.throws(() => decodeOutputAudio(frame(golden.header, new Uint8Array(1))));
});

test("accepts UTF8 turn identity, cue kind and maximum sequence", () => {
  const header = { ...golden.header, turn_id: "कॉल:1", sequence: 2**32 - 1, kind: "cue" };
  assert.deepEqual(decodeOutputAudio(frame(header)).header, header);
});

function audioFrame(generationId = "g-1", sequence = 0, turnId = "call:1") {
  return { header: { ...golden.header, generation_id: generationId, sequence, turn_id: turnId },
    payload: bytes(golden.payload_hex) };
}

function makePlaybackRig({ suspended = false } = {}) {
  const originalContext = globalThis.AudioContext;
  const originalFetch = globalThis.fetch;
  const scheduledSources: Array<{ buffer: AudioBuffer | null; stopped: boolean }> = [];
  let resolveResume = () => {};
  const resume = suspended ? new Promise<void>(resolve => { resolveResume = resolve; }) : Promise.resolve();
  class Context {
    state = suspended ? "suspended" : "running";
    currentTime = 0;
    destination = {};
    createGain() {
      return { connect() {}, gain: { value: 1, cancelScheduledValues() {}, setValueAtTime() {}, linearRampToValueAtTime() {} } };
    }
    async resume() { await resume; this.state = "running"; }
    async close() { this.state = "closed"; }
    createBuffer(_channels: number, count: number, rate: number) {
      assert.ok(count > 0, "Cannot schedule an empty audio buffer");
      return { duration: count / rate, getChannelData: () => new Float32Array(count) };
    }
    async decodeAudioData() { return this.createBuffer(1, 240, 24000); }
    createBufferSource() {
      const source = { buffer: null as AudioBuffer | null, stopped: false,
        onended: null as (() => void) | null, connect() {},
        start() { scheduledSources.push(source); },
        stop() { source.stopped = true; queueMicrotask(() => source.onended?.()); },
      };
      return source;
    }
  }
  globalThis.AudioContext = Context as unknown as typeof AudioContext;
  globalThis.fetch = async () => new Response(new Uint8Array(4));
  const queue = new PcmPlaybackQueue();
  return { queue, scheduledSources, resolveResume, restore: () => { globalThis.AudioContext = originalContext; globalThis.fetch = originalFetch; } };
}

test("a pending browser resume cannot schedule answer audio after invalidation", async () => {
  const rig = makePlaybackRig({ suspended: true });
  try {
    rig.queue.beginGeneration("call:1", "g-1");
    const pending = rig.queue.enqueue(audioFrame());
    rig.queue.invalidate();
    assert.equal(await Promise.race([pending, new Promise(resolve => setTimeout(() => resolve("stalled"), 100))]), null);
    rig.resolveResume();
    await pending;
    assert.equal(rig.scheduledSources.length, 0);
  } finally { await rig.queue.close(); rig.restore(); }
});


test("only contiguous current-generation frames schedule, and retired generations cannot restart", async () => {
  const rig = makePlaybackRig();
  try {
    rig.queue.beginGeneration("call:1", "g-1");
    assert.equal(await rig.queue.enqueue(audioFrame("g-1", 1)), null);
    assert.ok(await rig.queue.enqueue(audioFrame()));
    assert.equal(await rig.queue.enqueue(audioFrame()), null);
    assert.equal(await rig.queue.enqueue(audioFrame("g-1", 2)), null);
    assert.ok(await rig.queue.enqueue(audioFrame("g-1", 1)));
    assert.equal(rig.scheduledSources.length, 2);
    assert.equal(rig.queue.beginGeneration("call:2", "g-2"), true);
    assert.ok(rig.scheduledSources.every(source => source.stopped));
    assert.equal(await rig.queue.enqueue(audioFrame()), null);
    assert.equal(rig.queue.beginGeneration("call:1", "g-1"), false);
    assert.ok(await rig.queue.enqueue(audioFrame("g-2", 0, "call:2")));
  } finally { await rig.queue.close(); rig.restore(); }
});

test("pending local feedback cannot start after the answer generation is invalidated", async () => {
  const rig = makePlaybackRig({ suspended: true });
  try {
    rig.queue.beginGeneration("call:1", "g-1");
    await rig.queue.preloadFeedback("neutral_ack", "/bundled.wav");
    const pending = rig.queue.playFeedback("call:1", "g-1", "cue-1", "neutral_ack");
    rig.queue.invalidate();
    rig.resolveResume();
    assert.equal(await pending, null);
    assert.equal(rig.scheduledSources.length, 0);
  } finally { await rig.queue.close(); rig.restore(); }
});

test("closing releases pending audio without waiting for resume and prevents reactivation", async () => {
  const rig = makePlaybackRig({ suspended: true });
  try {
    rig.queue.beginGeneration("call:1", "g-1");
    const pending = rig.queue.enqueue(audioFrame());
    await rig.queue.close();
    assert.equal(await pending, null);
    assert.equal(rig.queue.beginGeneration("call:2", "g-2"), false);
    rig.resolveResume();
    assert.equal(rig.scheduledSources.length, 0);
  } finally { rig.restore(); }
});

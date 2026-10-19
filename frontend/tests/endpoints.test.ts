import assert from "node:assert/strict";
import test from "node:test";

import { resolveApiBaseUrl } from "../src/api/endpoints.ts";

test("production defaults API calls to the page origin", () => {
  assert.equal(resolveApiBaseUrl(undefined, "https://talkstream.example"), "https://talkstream.example");
  assert.equal(resolveApiBaseUrl("", "https://www.talkstream.example"), "https://www.talkstream.example");
});

test("an explicit development API URL still overrides the page origin", () => {
  assert.equal(
    resolveApiBaseUrl("http://127.0.0.1:8000/", "http://localhost:5173"),
    "http://127.0.0.1:8000",
  );
});

test('voice URLs contain a short ticket only and clear existing URL secrets', async () => {
  const endpoints = await import('../src/api/endpoints.ts');
  assert.equal(typeof endpoints.voiceWebSocketUrl, 'function');
  const url = new URL(endpoints.voiceWebSocketUrl('https://talkstream.example/?token=old#secret', 'ticket value'));
  assert.equal(url.protocol, 'wss:');
  assert.equal(url.pathname, '/ws/voice');
  assert.equal(url.searchParams.get('ticket'), 'ticket value');
  assert.equal(url.searchParams.has('token'), false);
  assert.equal(url.hash, '');
});

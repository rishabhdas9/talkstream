import assert from "node:assert/strict";
import test from "node:test";

import {
  AuthenticationExpiredError,
  errorForResponse,
  errorForVoiceClose,
} from "../src/api/errors.ts";

test("401 responses become a specific authentication-expired error", async () => {
  const error = await errorForResponse(
    new Response('{"detail":"Invalid or expired access token"}', {
      status: 401,
      headers: { "Content-Type": "application/json" },
    }),
  );

  assert.ok(error instanceof AuthenticationExpiredError);
  assert.equal(error.message, "Your session has expired. Please sign in again.");
});

test("voice close code 4401 becomes an authentication-expired error", () => {
  assert.ok(errorForVoiceClose(4401) instanceof AuthenticationExpiredError);
  assert.equal(errorForVoiceClose(1006).message, "Voice WebSocket connection failed");
});

test("non-authentication responses keep their API detail", async () => {
  const error = await errorForResponse(
    new Response('{"detail":"Recharge amount is too large"}', {
      status: 422,
      headers: { "Content-Type": "application/json" },
    }),
  );

  assert.equal(error.message, "Recharge amount is too large");
});

import assert from "node:assert/strict";
import test from "node:test";

import { componentHealth, formatMicroInr } from "../src/api/operations.ts";

const exa = { id: "web_search", provider: "exa", model: "fast", configured: true };
const observed = (last_outcome: string | null) => ({
  provider: "exa",
  model: "fast",
  attempts: 1,
  succeeded: last_outcome === "succeeded" ? 1 : 0,
  failed: 0,
  cancelled: 0,
  unknown: 0,
  last_outcome,
  last_attempt_at: "2026-10-18T10:00:00+00:00",
  last_success_at: null,
});

test("a configured key without observed requests is not reported healthy", () => {
  const health = componentHealth(exa, []);
  assert.equal(health.tone, "idle");
  assert.match(health.label, /no requests/i);
  assert.deepEqual(componentHealth({ ...exa, configured: false }, [observed("succeeded")]), {
    tone: "idle",
    label: "Not configured",
  });
});

test("health follows the last observed outcome for the same provider and model", () => {
  assert.equal(componentHealth(exa, [observed("succeeded")]).tone, "ok");
  assert.equal(componentHealth(exa, [observed("failed")]).tone, "down");
  assert.equal(componentHealth(exa, [observed("unknown")]).tone, "warn");
  assert.equal(componentHealth(exa, [{ ...observed("succeeded"), model: "deep" }]).tone, "idle");
});

test("components without recorded requests say so", () => {
  const email = { id: "email", provider: "resend", model: null, configured: true };
  assert.match(componentHealth(email, []).label, /not tracked/i);
});

test("provider micro-INR formats as rupees and unknown stays unknown", () => {
  assert.equal(formatMicroInr(5000), "₹0.0050");
  assert.equal(formatMicroInr(30_000_000), "₹30.0000");
  assert.equal(formatMicroInr(null), "unknown");
});

import assert from "node:assert/strict";
import test from "node:test";

import {
  modelsForProvider,
  selectedModelIsAvailable,
} from "../src/features/knowledge/logic.ts";

const providers = [
  {
    id: "openai",
    label: "OpenAI",
    available: true,
    models: [
      { id: "small", label: "Small", dimensions: 3, default: true },
      { id: "large", label: "Large", dimensions: 4, default: false },
    ],
  },
];

test("model choices come only from the selected backend provider", () => {
  assert.deepEqual(modelsForProvider(providers, "openai"), providers[0].models);
  assert.deepEqual(modelsForProvider(providers, "unknown"), []);
});

test("saving requires an available allowlisted model", () => {
  assert.equal(selectedModelIsAvailable(providers, "openai", "small"), true);
  assert.equal(selectedModelIsAvailable(providers, "openai", "made-up"), false);
  assert.equal(
    selectedModelIsAvailable([{ ...providers[0], available: false }], "openai", "small"),
    false,
  );
});

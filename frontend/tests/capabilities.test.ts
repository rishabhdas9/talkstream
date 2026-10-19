import assert from "node:assert/strict";
import test from "node:test";

import { knowledgeFeatureEnabled } from "../src/features/knowledge/capability.ts";

test("knowledge stays hidden until the backend explicitly enables it", () => {
  assert.equal(knowledgeFeatureEnabled(undefined), false);
  assert.equal(knowledgeFeatureEnabled({}), false);
  assert.equal(knowledgeFeatureEnabled({ features: {} }), false);
  assert.equal(knowledgeFeatureEnabled({ features: { knowledge_rag: false } }), false);
  assert.equal(knowledgeFeatureEnabled({ features: { knowledge_rag: true } }), true);
});

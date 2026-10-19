import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const appSource = readFileSync(new URL("../src/ui/App.tsx", import.meta.url), "utf8");

test("the frontend shell uses generic add-on hooks", () => {
  assert.doesNotMatch(appSource, /KnowledgePage|knowledgeFeatureEnabled|knowledge_rag/);
  assert.match(appSource, /ADDONS/);
});

test("the knowledge frontend is a self-contained feature module", () => {
  for (const path of ["api.ts", "logic.ts", "KnowledgePage.tsx", "index.tsx"]) {
    const source = readFileSync(
      new URL(`../src/features/knowledge/${path}`, import.meta.url),
      "utf8",
    );
    assert.ok(source.length > 0);
  }
});

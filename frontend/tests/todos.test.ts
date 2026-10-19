import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import test from "node:test";
import { executeTodoTool, todoRequestHash } from "../src/store/todoDb.ts";

const canonical = (name: string, args: Record<string, unknown>) =>
  createHash("sha256").update(JSON.stringify({ arguments: args, name })).digest("hex");

test("todo hash matches server canonical UTF8 and normalized arguments", async () => {
  for (const text of ["Buy milk", "दूध खरीदो", "milk लाना"]) {
    assert.equal(await todoRequestHash("todo_add", { text: ` ${text}  ` }), canonical("todo_add", { text }));
  }
  assert.equal(await todoRequestHash("todo_list", {}), canonical("todo_list", {}));
});

test("500 UTF16 unit truncation stays idempotent and never splits Unicode", async () => {
  for (const text of ["a".repeat(499) + " b", "a".repeat(499) + "😀", "a".repeat(499)]) {
    assert.equal(await todoRequestHash("todo_add", { text }), canonical("todo_add", { text: "a".repeat(499) }));
  }
  assert.equal(await todoRequestHash("todo_delete", { todo_id: " todo-42 " }), canonical("todo_delete", { todo_id: "todo-42" }));
  assert.equal(
    await todoRequestHash("todo_update", { todo_id: " todo-42 ", text: " buy  bananas " }),
    canonical("todo_update", { text: "buy bananas", todo_id: "todo-42" }),
  );
});

test("strict todo validation rejects unsupported or owner-bearing arguments", async () => {
  for (const args of [{ text: " " }, { text: 1 }, {}, { text: "milk", user_id: "other" }, { text: "\ud800" }]) {
    await assert.rejects(todoRequestHash("todo_add", args));
  }
  await assert.rejects(todoRequestHash("todo_list", { extra: true }));
  for (const args of [{ todo_id: "todo-42" }, { text: "milk" }, { todo_id: "todo-42", text: " " }]) {
    await assert.rejects(todoRequestHash("todo_update", args));
  }
  await assert.rejects(todoRequestHash("unknown" as never, {}));
  await assert.rejects(executeTodoTool({ userId: "", callId: "c", name: "todo_list", arguments: {} }));
  await assert.rejects(executeTodoTool({ userId: "a", callId: "", name: "todo_list", arguments: {} }));
});

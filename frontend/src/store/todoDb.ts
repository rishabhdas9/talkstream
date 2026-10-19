import type { Todo, TodoToolName } from "./todos";

export const TODO_DATABASE_NAME = "talkstream-todos";
export type TodoCommand = {
  userId: string;
  callId: string;
  name: TodoToolName;
  arguments: Record<string, unknown>;
  requestHash?: string;
  signal?: AbortSignal;
};
export type TodoResult =
  | { ok: true; todos: Todo[]; todo?: Todo; deleted?: Todo }
  | { ok: false; todos: Todo[]; error: string };
export type TodoReceipt = {
  call_id: string;
  request_hash: string;
  state: "succeeded" | "failed";
  result: TodoResult;
};
type StoredTodo = Todo & { userId: string };
type StoredReceipt = TodoReceipt & { userId: string };

function identity(value: string): void {
  if (typeof value !== "string" || !value.trim() || [...value].length > 128 || /\p{Surrogate}/u.test(value)) {
    throw new Error("Invalid todo identity");
  }
}

// Keys stay sorted so the hash matches the server's sort_keys canonical JSON.
const TOOL_KEYS: Record<TodoToolName, string[]> = {
  todo_add: ["text"], todo_list: [], todo_update: ["text", "todo_id"], todo_complete: ["todo_id"], todo_delete: ["todo_id"],
};

function argumentsFor(name: TodoToolName, args: Record<string, unknown>): Record<string, string> {
  const keys = Object.hasOwn(TOOL_KEYS, name) ? TOOL_KEYS[name] : undefined;
  if (keys === undefined || typeof args !== "object" || args === null || Array.isArray(args)) {
    throw new Error("Unsupported todo command");
  }
  if (Object.keys(args).length !== keys.length || keys.some(key => !Object.hasOwn(args, key))) {
    throw new Error("Invalid todo arguments");
  }
  const normalized: Record<string, string> = {};
  for (const key of keys) {
    const raw = args[key];
    if (typeof raw !== "string" || /\p{Surrogate}/u.test(raw)) throw new Error("Invalid todo argument");
    // Match S09's effective whitespace and UTF16-safe, idempotent text limit.
    let value = raw.replace(/[\s\u0085\u001c-\u001f]+/gu, " ").trim();
    if (key === "text") value = value.slice(0, 500).replace(/[\uD800-\uDBFF]$/u, "").trimEnd();
    if (!value || (key === "todo_id" && [...value].length > 128)) throw new Error("Invalid todo argument");
    normalized[key] = value;
  }
  return normalized;
}

async function requestHash(name: TodoToolName, args: Record<string, string>): Promise<string> {
  const bytes = new TextEncoder().encode(JSON.stringify({ arguments: args, name }));
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, "0")).join("");
}

export async function todoRequestHash(name: TodoToolName, args: Record<string, unknown>): Promise<string> {
  return requestHash(name, argumentsFor(name, args));
}

function openDatabase(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(TODO_DATABASE_NAME, 1);
    let blocked = false;
    request.onupgradeneeded = () => {
      const todos = request.result.createObjectStore("todos", { keyPath: ["userId", "id"] });
      todos.createIndex("userId", "userId");
      request.result.createObjectStore("receipts", { keyPath: ["userId", "call_id"] });
    };
    request.onerror = () => reject(request.error);
    request.onblocked = () => { blocked = true; reject(new Error("Todo storage is blocked; close other Talkstream tabs.")); };
    request.onsuccess = () => {
      if (blocked) { request.result.close(); return; }
      request.result.onversionchange = () => request.result.close();
      resolve(request.result);
    };
  });
}

async function transaction<T>(
  mode: IDBTransactionMode,
  signal: AbortSignal | undefined,
  run: (tx: IDBTransaction, result: (value: T) => void, fail: (error: unknown) => void) => void,
): Promise<T> {
  signal?.throwIfAborted();
  const db = await openDatabase();
  let abort: (() => void) | undefined;
  try {
    signal?.throwIfAborted();
    return await new Promise<T>((resolve, reject) => {
      const tx = db.transaction(["todos", "receipts"], mode);
      let value: T;
      let failure: unknown;
      abort = () => { try { tx.abort(); } catch { /* A completed commit is reconciled by receipt. */ } };
      signal?.addEventListener("abort", abort, { once: true });
      const fail = (error: unknown) => { failure = error; abort!(); };
      tx.oncomplete = () => signal?.aborted ? reject(signal.reason) : resolve(value);
      tx.onabort = () => reject(failure ?? signal?.reason ?? tx.error ?? new Error("Todo transaction aborted"));
      try { run(tx, result => { value = result; }, fail); } catch (error) { fail(error); }
    });
  } finally {
    if (abort) signal?.removeEventListener("abort", abort);
    db.close();
  }
}

function publicTodos(records: StoredTodo[]): Todo[] {
  return records.map(({ userId: _userId, ...todo }) => todo)
    .sort((a, b) => a.createdAt.localeCompare(b.createdAt) || a.id.localeCompare(b.id));
}

export async function loadTodos(userId: string): Promise<Todo[]> {
  identity(userId);
  return transaction("readonly", undefined, (tx, result) => {
    const request = tx.objectStore("todos").index("userId").getAll(userId);
    request.onsuccess = () => result(publicTodos(request.result));
  });
}

export async function readTodoReceipt(
  userId: string, callId: string, hash: string, signal?: AbortSignal,
): Promise<TodoReceipt | null> {
  identity(userId); identity(callId);
  if (!/^[a-f0-9]{64}$/.test(hash)) throw new Error("Invalid todo request hash");
  return transaction("readonly", signal, (tx, result, fail) => {
    const request = tx.objectStore("receipts").get([userId, callId]);
    request.onsuccess = () => {
      const saved = request.result as StoredReceipt | undefined;
      if (saved && saved.request_hash !== hash) { fail(new Error("Todo call ID conflicts with another request")); return; }
      if (!saved) { result(null); return; }
      const { userId: _owner, ...receipt } = saved;
      result(receipt);
    };
  });
}

export async function executeTodoTool(command: TodoCommand): Promise<TodoResult> {
  const { userId, callId, name, signal } = command;
  identity(userId); identity(callId);
  const args = argumentsFor(name, command.arguments);
  const hash = await requestHash(name, args);
  if (command.requestHash !== undefined && command.requestHash !== hash) throw new Error("Todo request hash mismatch");
  return transaction("readwrite", signal, (tx, result, fail) => {
    const todosStore = tx.objectStore("todos");
    const receipts = tx.objectStore("receipts");
    const prior = receipts.get([userId, callId]);
    prior.onsuccess = () => {
      const saved = prior.result as StoredReceipt | undefined;
      if (saved) {
        if (saved.request_hash !== hash) { fail(new Error("Todo call ID conflicts with another request")); return; }
        result(saved.result); return;
      }
      const request = todosStore.index("userId").getAll(userId);
      request.onsuccess = () => {
        try {
          const todos = publicTodos(request.result);
          let outcome: TodoResult = { ok: true, todos };
          if (name === "todo_add") {
            const todo: Todo = { id: crypto.randomUUID(), text: args.text, completed: false, createdAt: new Date().toISOString() };
            todosStore.put({ ...todo, userId });
            outcome = { ok: true, todo, todos: [...todos, todo] };
          } else if (name !== "todo_list") {
            const todo = todos.find(item => item.id === args.todo_id);
            if (!todo) outcome = { ok: false, error: "Todo was not found.", todos };
            else if (name === "todo_update") {
              const updated = { ...todo, text: args.text };
              todosStore.put({ ...updated, userId });
              outcome = { ok: true, todo: updated, todos: todos.map(item => item.id === todo.id ? updated : item) };
            } else if (name === "todo_complete") {
              const completed = { ...todo, completed: true };
              todosStore.put({ ...completed, userId });
              outcome = { ok: true, todo: completed, todos: todos.map(item => item.id === todo.id ? completed : item) };
            } else {
              todosStore.delete([userId, todo.id]);
              outcome = { ok: true, deleted: todo, todos: todos.filter(item => item.id !== todo.id) };
            }
          }
          receipts.put({ userId, call_id: callId, request_hash: hash, state: outcome.ok ? "succeeded" : "failed", result: outcome } satisfies StoredReceipt);
          result(outcome);
        } catch (error) { fail(error); }
      };
    };
  });
}

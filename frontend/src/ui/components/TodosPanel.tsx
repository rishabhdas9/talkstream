import { useState } from "react";

import type { Todo, TodoToolName } from "../../store/todos";

type TodosPanelProps = {
  todos: Todo[];
  agentToast: string;
  error: string;
  onRunTool: (name: TodoToolName, args: Record<string, unknown>) => Promise<boolean>;
  onCollapse: () => void;
};

export function TodosPanel({ todos, agentToast, error, onRunTool, onCollapse }: TodosPanelProps) {
  const [newTodo, setNewTodo] = useState("");
  const [pending, setPending] = useState(false);

  async function addTodo() {
    const text = newTodo.trim();
    if (!text || pending) return;
    setPending(true);
    try {
      if (await onRunTool("todo_add", { text })) setNewTodo("");
    } finally { setPending(false); }
  }

  return (
    <aside className="call-side-panel todos-panel" aria-label="Todos"
      style={{
        width: 290,
        flexShrink: 0,
        borderLeft: "1px solid #E5E5E1",
        background: "#FFFFFF",
        display: "flex",
        flexDirection: "column",
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          padding: "16px 18px 12px",
        }}
      >
        <div style={{ fontSize: 13, fontWeight: 600 }}>
          Todos <span style={{ color: "#A6A6A0", fontWeight: 500 }}>{todos.length}</span>
        </div>
        <button
          type="button"
          onClick={onCollapse}
          title="Collapse panel"
          style={{
            border: "none",
            background: "none",
            color: "#6B6B66",
            fontSize: 15,
            padding: "2px 6px",
          }}
        >
          ›
        </button>
      </div>

      {agentToast && (
        <div
          style={{
            margin: "0 18px 10px",
            padding: "8px 12px",
            borderRadius: 8,
            background: "#EEF2FE",
            color: "#3A57D4",
            fontSize: 12,
            animation: "fadeUp .25s ease",
          }}
        >
          {agentToast}
        </div>
      )}

      {error && <div role="alert" style={{ margin: "0 18px 10px", color: "#B3352E", fontSize: 12 }}>{error}</div>}
      <div style={{ display: "flex", gap: 8, padding: "0 18px 12px" }}>
        <input
          value={newTodo}
          disabled={pending}
          maxLength={500}
          aria-label="Todo text"
          onChange={(event) => setNewTodo(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") void addTodo();
          }}
          placeholder="Add a todo"
          className="inputFocus"
          style={{
            flex: 1,
            minWidth: 0,
            border: "1px solid #E5E5E1",
            borderRadius: 999,
            padding: "7px 14px",
            fontSize: 13,
            background: "#F7F7F5",
          }}
        />
        <button
          type="button"
          onClick={() => void addTodo()}
          disabled={pending}
          title="Add"
          className="hovDark"
          style={{
            width: 32,
            height: 32,
            borderRadius: "50%",
            border: "none",
            background: "#111110",
            color: "#FFFFFF",
            fontSize: 16,
            lineHeight: 1,
            flexShrink: 0,
          }}
        >
          +
        </button>
      </div>

      <div style={{ flex: 1, overflowY: "auto", padding: "0 10px 12px" }}>
        {todos.map((todo) => (
          <div
            key={todo.id}
            className="hovBgSoft"
            style={{
              display: "flex",
              alignItems: "center",
              gap: 10,
              padding: "9px 8px",
              borderRadius: 8,
            }}
          >
            <button
              type="button"
              onClick={() => {
                if (!todo.completed) void onRunTool("todo_complete", { todo_id: todo.id });
              }}
              style={{
                width: 18,
                height: 18,
                borderRadius: "50%",
                border: `1.5px solid ${todo.completed ? "#111110" : "#C8C8C2"}`,
                background: todo.completed ? "#111110" : "transparent",
                color: "#FFFFFF",
                fontSize: 10,
                lineHeight: 1,
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                padding: 0,
                flexShrink: 0,
                cursor: todo.completed ? "default" : "pointer",
              }}
            >
              {todo.completed ? "✓" : ""}
            </button>
            <div
              style={{
                flex: 1,
                fontSize: 13,
                lineHeight: 1.4,
                color: todo.completed ? "#A6A6A0" : "#111110",
                textDecoration: todo.completed ? "line-through" : "none",
              }}
            >
              {todo.text}
            </div>
            <button
              type="button"
              onClick={() => void onRunTool("todo_delete", { todo_id: todo.id })}
              title="Delete"
              className="hovRed"
              style={{
                border: "none",
                background: "none",
                color: "#C8C8C2",
                fontSize: 14,
                padding: "0 2px",
              }}
            >
              ×
            </button>
          </div>
        ))}
        {todos.length === 0 && (
          <div
            style={{
              padding: "24px 12px",
              textAlign: "center",
              fontSize: 12.5,
              color: "#A6A6A0",
              lineHeight: 1.6,
            }}
          >
            No todos yet — try saying
            <br />
            "add buy milk to my list".
          </div>
        )}
      </div>

      <div
        style={{
          padding: "10px 18px",
          borderTop: "1px solid #F0F0EC",
          fontSize: 11,
          color: "#A6A6A0",
        }}
      >
        Stored in this browser only
      </div>
    </aside>
  );
}

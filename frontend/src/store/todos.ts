export type Todo = {
  id: string;
  text: string;
  completed: boolean;
  createdAt: string;
};

export type TodoToolName = "todo_add" | "todo_list" | "todo_update" | "todo_complete" | "todo_delete";

export { executeTodoTool, loadTodos, readTodoReceipt, todoRequestHash } from "./todoDb";
export type { TodoCommand, TodoResult, TodoReceipt } from "./todoDb";

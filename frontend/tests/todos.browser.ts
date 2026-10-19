import { executeTodoTool, loadTodos, readTodoReceipt, todoRequestHash, TODO_DATABASE_NAME } from '../src/store/todoDb';

export async function runTodoStorageChecks(): Promise<string[]> {
  const checks: string[] = [];
  const user = crypto.randomUUID();
  const other = crypto.randomUUID();
  const check = (value: unknown, message: string) => { if (!value) throw new Error(message); };
  const reject = async (promise: Promise<unknown>) => {
    let rejected = false; try { await promise; } catch { rejected = true; }
    check(rejected, 'Operation should reject');
  };
  const command = (callId: string, text = 'milk') => ({ userId: user, callId, name: 'todo_add' as const, arguments: { text } });
  const legacyBefore = localStorage.getItem('talkstream_todos');
  const put = IDBObjectStore.prototype.put;
  try {
    localStorage.setItem('talkstream_todos', JSON.stringify([{ id: 'old', text: 'private legacy', completed: false, createdAt: new Date().toISOString() }]));
    check((await loadTodos(user)).length === 0, 'Legacy device data was imported');
    checks.push('no automatic legacy import');
    const first = await executeTodoTool(command('same'));
    const replay = await executeTodoTool(command('same'));
    check(JSON.stringify(first) === JSON.stringify(replay), 'Replay changed the saved result');
    check(first.ok && first.todo?.text === 'milk' && (await loadTodos(user)).length === 1, 'Redelivery repeated add');
    check((await loadTodos(other)).length === 0, 'Other user read todos');
    checks.push('committed redelivery and user isolation');
    const concurrent = await Promise.all(Array.from({ length: 8 }, () => executeTodoTool(command('concurrent'))));
    check(concurrent.every(result => JSON.stringify(result) === JSON.stringify(concurrent[0])), 'Concurrent receipts diverged');
    check((await loadTodos(user)).length === 2, 'Concurrent duplicates repeated add');
    checks.push('concurrent duplicate serialization');
    const conflicts = await Promise.allSettled([executeTodoTool(command('conflict', 'one')), executeTodoTool(command('conflict', 'two'))]);
    check(conflicts.filter(value => value.status === 'fulfilled').length === 1, 'Conflicting concurrent commands both ran');
    check((await loadTodos(user)).length === 3, 'Conflicting command changed data');
    checks.push('concurrent argument conflict');
    const hash = await todoRequestHash('todo_add', { text: 'milk' });
    const saved = await readTodoReceipt(user, 'same', hash);
    check(saved?.state === 'succeeded' && JSON.stringify(saved.result) === JSON.stringify(first), 'Committed receipt unavailable after lost ack');
    check(await readTodoReceipt(other, 'same', hash) === null, 'Other user read receipt');
    await reject(readTodoReceipt(user, 'same', '0'.repeat(64)));
    await reject(executeTodoTool({ ...command('wrong-hash'), requestHash: '0'.repeat(64) }));
    checks.push('read-only receipt reconciliation and hash validation');
    const todoId = first.ok ? first.todo!.id : '';
    const updated = await executeTodoTool({ userId: user, callId: 'update', name: 'todo_update', arguments: { todo_id: todoId, text: 'oat milk' } });
    check(updated.ok && updated.todo?.text === 'oat milk' && !updated.todo.completed, 'Update failed');
    const complete = { userId: user, callId: 'complete', name: 'todo_complete' as const, arguments: { todo_id: todoId } };
    const completed = await executeTodoTool(complete);
    check(completed.ok && completed.todo?.completed, 'Complete failed');
    const deleted = await executeTodoTool({ ...complete, callId: 'delete', name: 'todo_delete' });
    check(deleted.ok && deleted.deleted?.id === todoId, 'Delete failed');
    check(JSON.stringify(await executeTodoTool(complete)) === JSON.stringify(completed), 'Replay lost original result after deletion');
    const missing = await executeTodoTool({ ...complete, callId: 'missing' });
    check(!missing.ok && (await loadTodos(user)).length === 2, 'Missing item invented success');
    check((await readTodoReceipt(user, 'missing', await todoRequestHash('todo_complete', complete.arguments)))?.state === 'failed', 'Failure receipt not saved');
    const list = await executeTodoTool({ userId: user, callId: 'list', name: 'todo_list', arguments: {} });
    check(list.ok && list.todos.length === 2, 'List did not use shared store');
    await executeTodoTool({ ...command('same'), userId: other });
    check((await loadTodos(other)).length === 1 && (await loadTodos(user)).length === 2, 'Same ID crossed accounts');
    checks.push('complete delete list and immutable outcome receipts');
    IDBObjectStore.prototype.put = function(...args: Parameters<IDBObjectStore['put']>) {
      if (this.name === 'receipts') throw new DOMException('Storage quota exceeded', 'QuotaExceededError');
      return put.apply(this, args);
    };
    await reject(executeTodoTool(command('quota', 'must roll back')));
    IDBObjectStore.prototype.put = put;
    check((await loadTodos(user)).length === 2, 'Quota failure partially committed mutation');
    check(await readTodoReceipt(user, 'quota', await todoRequestHash('todo_add', { text: 'must roll back' })) === null, 'Aborted receipt survived');
    check((await executeTodoTool(command('quota', 'must roll back'))).ok, 'Aborted command could not retry safely');
    checks.push('real transaction quota rollback');
    const controller = new AbortController();
    IDBObjectStore.prototype.put = function(...args: Parameters<IDBObjectStore['put']>) {
      const result = put.apply(this, args);
      if (this.name === 'todos') controller.abort();
      return result;
    };
    await reject(executeTodoTool({ ...command('cancel'), signal: controller.signal }));
    IDBObjectStore.prototype.put = put;
    check((await loadTodos(user)).length === 3, 'Cancelled transaction changed data');
    check(await readTodoReceipt(user, 'cancel', hash) === null, 'Cancelled receipt committed');
    const preCancelled = new AbortController(); preCancelled.abort();
    await reject(executeTodoTool({ ...command('pre-cancel'), signal: preCancelled.signal }));
    checks.push('abort before and during actual transaction');
    return checks;
  } finally {
    IDBObjectStore.prototype.put = put;
    if (legacyBefore === null) localStorage.removeItem('talkstream_todos'); else localStorage.setItem('talkstream_todos', legacyBefore);
    await new Promise<void>((resolve, reject) => {
      const request = indexedDB.deleteDatabase(TODO_DATABASE_NAME);
      request.onsuccess = () => resolve(); request.onerror = () => reject(request.error);
    });
  }
}

import { VoiceClient } from '../src/voice/client';
import { MicrophoneCapture } from '../src/audio/capture';
import { parseServerEvent } from '../src/voice/protocol';

export async function runTodoVoiceChecks(target = 'all'): Promise<string[]> {
  const oldFetch = window.fetch, oldSocket = window.WebSocket;
  const oldStart = MicrophoneCapture.prototype.start, oldStop = MicrophoneCapture.prototype.stop;
  const oldPut = IDBObjectStore.prototype.put;
  const oldTransaction = IDBDatabase.prototype.transaction;
  const oldLegacy = localStorage.getItem('talkstream_todos');
  const checks: string[] = [], userId = crypto.randomUUID();
  const committed = new Set<string>();
  const completedTransactions = new Set<IDBTransaction>();
  const writes = new Map<string, IDBTransaction>();
  const tick = () => new Promise(resolve => setTimeout(resolve, 0));
  const check = (value: unknown, message: string) => { if (!value) throw new Error(message); };
  async function until(predicate: () => boolean) {
    const end = performance.now() + 3000;
    while (!predicate()) { if (performance.now() > end) throw new Error('Timed out waiting for todo result'); await tick(); }
  }
  let socket: Socket, client: VoiceClient | null = null;
  let acks: Array<Record<string, any>> = [], updates: unknown[] = [], errors: string[] = [];
  let authRequests = 0, authMode = 'ok', releaseAuth: (() => void) | undefined;
  let retireOnWrite = '', premature = false, retireAfterCommit = false;
  class Socket {
    static OPEN = 1; static CONNECTING = 0;
    readyState = 0; onopen: (() => void) | null = null;
    onmessage: ((event: MessageEvent) => void) | null = null;
    onclose: ((event: { code: number }) => void) | null = null;
    constructor(_url: string) { socket = this; queueMicrotask(() => { this.readyState = 1; this.onopen?.(); }); }
    send(raw: string | ArrayBuffer) {
      if (typeof raw !== 'string') return;
      const message = JSON.parse(raw);
      if (message.type === 'end_call') queueMicrotask(() => this.control({ type: 'call_ended', reason: 'user' }));
      if (message.type === 'tool_result') {
        if (message.result?.ok && !completedTransactions.has(writes.get(message.call_id)!)) premature = true;
        acks.push(message);
      }
    }
    control(value: unknown) { this.onmessage?.({ data: JSON.stringify(value) } as MessageEvent); }
    close() { if (this.readyState === 3) return; this.readyState = 3; this.onclose?.({ code: 1000 }); }
  }
  const generation = { turn_id: 'call:1', generation_id: 'g-1' };
  const interrupt = () => socket.control({ type: 'interrupt', ...generation, reason: 'barge_in' });
  const request = async (callId: string, text = 'milk') => ({ type: 'tool_request', ...generation,
    call_id: callId, name: 'todo_add', arguments: { text }, request_hash: await todoRequestHash('todo_add', { text }) });
  async function start() {
    acks = []; updates = []; errors = []; premature = false;
    client = new VoiceClient('fixture-token', {
      onState() {}, onTranscript() {}, onAgentText() {}, onMetrics() {}, onInterrupted() {}, onBilling() {},
      onTodosChanged: todos => updates.push(todos), onError: message => errors.push(message),
    });
    await client.start();
    socket!.control({ type: 'ready', session_id: 'call', balance_paise: 500, capabilities: ['audio_frames_v2'] });
    socket!.control({ type: 'final_transcript', ...generation, text: 'Add milk' });
  }
  try {
    MicrophoneCapture.prototype.start = async () => {};
    MicrophoneCapture.prototype.stop = async () => {};
    window.WebSocket = Socket as never;
    window.fetch = (async (url: string, options?: RequestInit) => {
      if (String(url).endsWith('/auth/me')) {
        authRequests += 1;
        check(new Headers(options?.headers).get('Authorization') === 'Bearer fixture-token', 'Owner lookup not authenticated');
        if (authMode === 'wait') await new Promise<void>(resolve => { releaseAuth = resolve; });
        if (authMode === 'reject') return new Response('{}', { status: 401 });
        return new Response(JSON.stringify({ id: userId }));
      }
      return new Response(JSON.stringify({ ticket: 'fixture-ticket' }));
    }) as typeof fetch;
    IDBDatabase.prototype.transaction = function(...args: Parameters<IDBDatabase['transaction']>) {
      const tx = oldTransaction.apply(this, args);
      // Observe completion before the store's promise handler can resume its caller.
      tx.addEventListener('complete', () => {
        completedTransactions.add(tx);
        if (retireAfterCommit && tx.mode === 'readwrite') {
          retireAfterCommit = false;
          void client!.stop();
        }
      });
      return tx;
    };
    IDBObjectStore.prototype.put = function(...args: Parameters<IDBObjectStore['put']>) {
      if (this.name === 'receipts') {
        const id = (args[0] as Record<string, string>).call_id;
        writes.set(id, this.transaction);
        this.transaction.addEventListener('complete', () => committed.add(id), { once: true });
        if (retireOnWrite === 'quota') throw new DOMException('Quota exceeded', 'QuotaExceededError');
      }
      const result = oldPut.apply(this, args);
      if (this.name === 'todos' && ['interrupt', 'stop'].includes(retireOnWrite)) {
        const mode = retireOnWrite; retireOnWrite = '';
        if (mode === 'interrupt') interrupt();
        else if (mode === 'stop') void client!.stop();
      }
      return result;
    };
    const run = async (name: string, action: () => Promise<void>) => {
      if (target !== 'all' && target !== name) return;
      await start(); try { await action(); checks.push(name); } finally { await client?.stop(); }
    };
    await run('commit and replay', async () => {
      const message = await request('commit'); socket!.control(message);
      await until(() => acks.length === 1);
      check(!premature && acks[0].result.ok, `Commit acknowledgement invalid: ${JSON.stringify({ premature, ack: acks[0], errors, authRequests })}`);
      await until(() => updates.length === 1);
      socket!.control(message); await until(() => acks.length === 2);
      check(JSON.stringify(acks[0].result) === JSON.stringify(acks[1].result), 'Client replay changed result');
      check((await loadTodos(userId)).length === 1 && authRequests === 1, 'Client repeated mutation or owner lookup');
    });
    await run('lost ack query', async () => {
      await executeTodoTool({ userId, callId: 'lost', name: 'todo_add', arguments: { text: 'lost' } });
      socket!.control({ type: 'tool_receipt_query', call_id: 'lost', request_id: 'query', request_hash: await todoRequestHash('todo_add', { text: 'lost' }) });
      await until(() => acks.length === 1);
      check(acks[0].call_id === 'query' && acks[0].result.call_id === 'lost' && acks[0].result.state === 'succeeded', 'Query did not return saved receipt with separate correlation');
      socket!.control({ type: 'tool_receipt_query', call_id: 'absent', request_id: 'missing', request_hash: '0'.repeat(64) });
      await until(() => acks.length === 2); check(acks[1].result === null, 'Missing receipt invented completion');
    });
    await run('stop after commit', async () => {
      retireAfterCommit = true;
      const message = await request('committed-lost'); socket!.control(message);
      await until(() => !retireAfterCommit); await tick();
      check(acks.length === 0 && updates.length === 0, 'Retired committed action acknowledged');
      check((await readTodoReceipt(userId, 'committed-lost', message.request_hash))?.state === 'succeeded', 'Commit was lost during stop');
      await client!.stop(); await start();
      socket!.control({ type: 'tool_receipt_query', call_id: 'committed-lost', request_id: 'recover', request_hash: message.request_hash });
      await until(() => acks.length === 1);
      check(acks[0].result.state === 'succeeded', 'Next client could not reconcile committed action');
    });
    await run('retired query', async () => {
      authMode = 'wait';
      socket!.control({ type: 'tool_receipt_query', call_id: 'lost', request_id: 'retired-query', request_hash: '0'.repeat(64) });
      await until(() => releaseAuth !== undefined); interrupt();
      releaseAuth!(); releaseAuth = undefined; authMode = 'ok';
      await tick(); await tick();
      check(acks.length === 0 && updates.length === 0, 'Retired query acknowledged');
    });
    for (const mode of ['interrupt', 'stop']) {
      await run(`${mode} pending auth`, async () => {
        authMode = 'wait'; socket!.control(await request(`auth-${mode}`));
        await until(() => releaseAuth !== undefined);
        if (mode === 'stop') void client!.stop(); else interrupt();
        releaseAuth!(); releaseAuth = undefined; authMode = 'ok';
        await tick(); await tick();
        check(acks.length === 0 && updates.length === 0 && await readTodoReceipt(userId, `auth-${mode}`, await todoRequestHash('todo_add', { text: 'milk' })) === null, 'Retired auth continuation mutated or acknowledged');
      });
      await run(`${mode} during write`, async () => {
        retireOnWrite = mode; socket!.control(await request(`write-${mode}`));
        await until(() => retireOnWrite === ''); await tick(); await tick();
        check(acks.length === 0 && updates.length === 0 && await readTodoReceipt(userId, `write-${mode}`, await todoRequestHash('todo_add', { text: 'milk' })) === null, 'Retired write committed or acknowledged');
      });
    }
    await run('late old generation', async () => {
      interrupt(); socket!.control(await request('late-old')); await tick(); await tick();
      check(acks.length === 0 && updates.length === 0, 'Old generation executed after interruption');
      socket!.control({ type: 'final_transcript', turn_id: 'call:2', generation_id: 'g-2', text: 'New task' });
      const message = { ...await request('current'), turn_id: 'call:2', generation_id: 'g-2' };
      socket!.control(message); await until(() => acks.length === 1); check(acks[0].result.ok, 'New generation could not execute');
    });
    await run('hash mismatch', async () => {
      socket!.control({ ...await request('bad-hash'), request_hash: '0'.repeat(64) });
      await until(() => acks.length === 1);
      check(acks[0].result.ok === false && !committed.has('bad-hash'), 'Mismatched hash executed');
    });
    await run('storage failure', async () => {
      retireOnWrite = 'quota'; socket!.control(await request('quota-client'));
      await until(() => acks.length === 1); retireOnWrite = '';
      check(acks[0].result.ok === false && !committed.has('quota-client') && errors.length > 0, 'Storage failure claimed success or stayed invisible');
    });
    await run('identity failure', async () => {
      authMode = 'reject'; socket!.control(await request('identity-fail'));
      await until(() => errors.length > 0); authMode = 'ok';
      check(!committed.has('identity-fail') && !acks.some(message => message.result?.ok), 'Unauthenticated owner executed');
    });
    if (target === 'all' || target === 'protocol') {
      const valid = await request('protocol');
      for (const field of ['call_id', 'turn_id', 'generation_id', 'request_hash', 'arguments', 'name']) {
        let rejected = false; try { parseServerEvent(JSON.stringify({ ...valid, [field]: null })); } catch { rejected = true; }
        check(rejected, `Malformed tool field ${field} accepted`);
      }
      checks.push('strict tool wire parser');
    }
    return checks;
  } finally {
    await client?.stop(); IDBObjectStore.prototype.put = oldPut;
    IDBDatabase.prototype.transaction = oldTransaction;
    window.fetch = oldFetch; window.WebSocket = oldSocket;
    MicrophoneCapture.prototype.start = oldStart; MicrophoneCapture.prototype.stop = oldStop;
    if (oldLegacy === null) localStorage.removeItem('talkstream_todos'); else localStorage.setItem('talkstream_todos', oldLegacy);
    await new Promise<void>((resolve, reject) => { const r = indexedDB.deleteDatabase(TODO_DATABASE_NAME); r.onsuccess = () => resolve(); r.onerror = () => reject(r.error); });
  }
}

import { createElement } from 'react';
import { createRoot } from 'react-dom/client';
import { useVoiceSession } from '../src/ui/hooks/useVoiceSession';
import { App as LegacyApp } from '../src/legacy/App';
import { TodosPanel } from '../src/ui/components/TodosPanel';

export async function runTodoInterfaceChecks(): Promise<string[]> {
  const oldFetch = window.fetch, oldPut = IDBObjectStore.prototype.put;
  const oldGetAll = IDBIndex.prototype.getAll;
  const oldToken = localStorage.getItem('talkstream_token'), oldLegacy = localStorage.getItem('talkstream_todos');
  const host = document.createElement('div'); document.body.append(host);
  const root = createRoot(host), checks: string[] = [];
  const userA = crypto.randomUUID(), userB = crypto.randomUUID();
  const tick = () => new Promise(resolve => setTimeout(resolve, 0));
  const check = (value: unknown, message: string) => { if (!value) throw new Error(message); };
  async function until(predicate: () => boolean) {
    const end = performance.now() + 3000;
    while (!predicate()) { if (performance.now() > end) throw new Error('Timed out waiting for account state'); await tick(); }
  }
  let hook: ReturnType<typeof useVoiceSession>, releaseA: (() => void) | undefined;
  let waitA = false, failWrite = false, logoutOnWrite = false, failRefresh = false, refreshOnWrite = false;
  function Harness({ token }: { token: string }) {
    hook = useVoiceSession(token);
    return createElement(TodosPanel, { todos: hook.state.todos, error: hook.state.todoError,
      agentToast: '', onRunTool: hook.runTodoTool, onCollapse() {} });
  }
  const render = async (token: string) => { root.render(createElement(Harness, { token })); await tick(); await tick(); };
  try {
    window.fetch = (async (url: string, options?: RequestInit) => {
      if (String(url).endsWith('/auth/me')) {
        const token = new Headers(options?.headers).get('Authorization');
        if (token === 'Bearer A' && waitA) await new Promise<void>(resolve => { releaseA = resolve; });
        return new Response(JSON.stringify({ id: token === 'Bearer A' ? userA : userB }));
      }
      return new Response(JSON.stringify({ balance_paise: 500, transactions: [], addons: {} }));
    }) as typeof fetch;
    localStorage.setItem('talkstream_todos', JSON.stringify([{ id: 'legacy', text: 'legacy secret', completed: false, createdAt: new Date().toISOString() }]));
    await executeTodoTool({ userId: userA, callId: 'seed-a', name: 'todo_add', arguments: { text: 'only A' } });
    await executeTodoTool({ userId: userB, callId: 'seed-b', name: 'todo_add', arguments: { text: 'only B' } });
    await render('A'); await until(() => hook!.state.todos.length > 0);
    check(hook!.state.todos.length === 1 && hook!.state.todos[0].text === 'only A', 'Initial UI read imported legacy/other-account data');
    checks.push('authenticated initial load');
    check(await hook!.runTodoTool('todo_add', { text: 'manual A' }), 'Manual add failed');
    await until(() => hook!.state.todos.length === 2);
    const manual = hook!.state.todos.find(todo => todo.text === 'manual A')!;
    check((await loadTodos(userA)).some(todo => todo.id === manual.id) && (await loadTodos(userB)).length === 1, 'Manual edit used a separate or wrong-owner store');
    check(await hook!.runTodoTool('todo_complete', { todo_id: manual.id }), 'Manual complete failed');
    check(await hook!.runTodoTool('todo_delete', { todo_id: manual.id }), 'Manual delete failed');
    await until(() => hook!.state.todos.length === 1);
    checks.push('manual add complete delete share receipt store');
    IDBObjectStore.prototype.put = function(...args: Parameters<IDBObjectStore['put']>) {
      if (this.name === 'receipts' && failWrite) throw new DOMException('Quota exceeded', 'QuotaExceededError');
      if (this.name === 'receipts' && refreshOnWrite) { refreshOnWrite = false; failRefresh = true; }
      const result = oldPut.apply(this, args);
      if (this.name === 'todos' && logoutOnWrite) {
        logoutOnWrite = false;
        void hook!.stop({ discardFinalBilling: true });
        root.render(createElement(Harness, { token: '' }));
      }
      return result;
    };
    IDBIndex.prototype.getAll = function(...args: Parameters<IDBIndex['getAll']>) {
      if (failRefresh && this.objectStore.transaction.mode === 'readonly') {
        failRefresh = false; throw new DOMException('Read failed', 'UnknownError');
      }
      return oldGetAll.apply(this, args);
    };
    const input = host.querySelector('input[aria-label="Todo text"]') as HTMLInputElement;
    const addButton = host.querySelector('button[title="Add"]') as HTMLButtonElement;
    const type = async (value: string) => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, value);
      input.dispatchEvent(new Event('input', { bubbles: true })); await tick();
    };
    failWrite = true;
    await type('must fail'); addButton.click();
    await until(() => Boolean(hook!.state.todoError)); failWrite = false;
    check(input.value === 'must fail' && host.querySelector('[role="alert"]')?.textContent?.includes('Could not save'), 'Failure erased input or stayed invisible');
    check((await loadTodos(userA)).length === 1, 'Failed manual transaction survived');
    checks.push('visible manual storage failure retains typed input');
    addButton.click(); await until(() => input.value === '' && hook!.state.todos.length === 2);
    check(await hook!.runTodoTool('todo_delete', { todo_id: hook!.state.todos.find(todo => todo.text === 'must fail')!.id }), 'Panel retry did not save');
    await until(() => hook!.state.todos.length === 1);
    checks.push('successful panel retry clears input');
    await type('saved before refresh failure'); refreshOnWrite = true; addButton.click();
    await until(() => input.value === '' && hook!.state.todoError.includes('saved'));
    const saved = (await loadTodos(userA)).find(todo => todo.text === 'saved before refresh failure');
    check(saved, 'Refresh failure lost committed mutation');
    check(await hook!.runTodoTool('todo_delete', { todo_id: saved!.id }), 'Committed result could not be removed');
    await until(() => hook!.state.todos.length === 1);
    checks.push('committed manual success survives refresh-only failure');
    logoutOnWrite = true;
    check(await hook!.runTodoTool('todo_add', { text: 'must cancel' }) === false, 'Logout did not retire manual command');
    await until(() => hook!.state.todos.length === 0);
    check((await loadTodos(userA)).length === 1, 'Logout committed pending manual command');
    checks.push('logout aborts actual pending manual write');
    waitA = true; await render('A'); await until(() => releaseA !== undefined);
    await render('B'); await until(() => hook!.state.todos.some(todo => todo.text === 'only B'));
    releaseA!(); releaseA = undefined; waitA = false; await tick(); await tick();
    check(hook!.state.todos.length === 1 && hook!.state.todos[0].text === 'only B', 'Late A load overwrote B');
    checks.push('rapid account switch rejects late owner load');
    root.render(createElement('div')); await tick();
    localStorage.setItem('talkstream_token', 'A'); root.render(createElement(LegacyApp));
    await until(() => host.textContent?.includes('only A') === true);
    check(!host.textContent?.includes('only B') && !host.textContent?.includes('legacy secret'), 'Legacy UI crossed owners/imported legacy data');
    const logout = [...host.querySelectorAll('button')].find(button => button.textContent === 'Log out')!;
    logout.click(); await tick(); check(!host.textContent?.includes('only A'), 'Legacy logout retained private todos');
    checks.push('legacy authenticated load and logout');
    return checks;
  } finally {
    root.unmount(); host.remove(); IDBObjectStore.prototype.put = oldPut; IDBIndex.prototype.getAll = oldGetAll; window.fetch = oldFetch;
    if (oldToken === null) localStorage.removeItem('talkstream_token'); else localStorage.setItem('talkstream_token', oldToken);
    if (oldLegacy === null) localStorage.removeItem('talkstream_todos'); else localStorage.setItem('talkstream_todos', oldLegacy);
    await new Promise<void>((resolve, reject) => { const r = indexedDB.deleteDatabase(TODO_DATABASE_NAME); r.onsuccess = () => resolve(); r.onerror = () => reject(r.error); });
  }
}

import { App as ModernApp } from '../src/ui/App';

export async function runTodoAppRetirementChecks(): Promise<string[]> {
  const oldFetch = window.fetch, oldPut = IDBObjectStore.prototype.put;
  const oldAbort = IDBTransaction.prototype.abort, oldRemove = Storage.prototype.removeItem;
  const oldToken = localStorage.getItem('talkstream_token'), oldEmail = localStorage.getItem('talkstream_email');
  const oldUrl = location.pathname;
  const host = document.createElement('div'); document.body.append(host);
  let root: ReturnType<typeof createRoot> | null = null;
  const tick = () => new Promise(resolve => setTimeout(resolve, 0));
  const checks: string[] = [];
  const check = (value: unknown, message: string) => { if (!value) throw new Error(message); };
  async function until(predicate: () => boolean) {
    const end = performance.now() + 3000;
    while (!predicate()) { if (performance.now() > end) throw new Error('Timed out waiting for app retirement'); await tick(); }
  }
  try {
    for (const mode of ['logout', 'expiry']) {
      const userId = crypto.randomUUID(), order: string[] = [];
      let releaseWallet: ((response: Response) => void) | undefined, fired = false;
      localStorage.setItem('talkstream_token', 'A');
      window.fetch = (async (url: string) => {
        if (String(url).endsWith('/auth/me')) return new Response(JSON.stringify({ id: userId }));
        if (String(url).endsWith('/wallet')) return await new Promise<Response>(resolve => { releaseWallet = resolve; });
        return new Response(JSON.stringify({ features: [], addons: [] }));
      }) as typeof fetch;
      await executeTodoTool({ userId, callId: 'root-seed', name: 'todo_add', arguments: { text: 'root owner' } });
      IDBTransaction.prototype.abort = function() { order.push('abort'); return oldAbort.call(this); };
      Storage.prototype.removeItem = function(key: string) { if (this === localStorage && key === 'talkstream_token') order.push('clear'); oldRemove.call(this, key); };
      IDBObjectStore.prototype.put = function(...args: Parameters<IDBObjectStore['put']>) {
        const result = oldPut.apply(this, args);
        if (this.name === 'todos' && !fired && (args[0] as { text: string }).text === 'retire me') {
          fired = true;
          if (mode === 'logout') [...host.querySelectorAll('button')].find(button => button.textContent === 'Log out')!.click();
          else releaseWallet!(new Response('{}', { status: 401 }));
        }
        return result;
      };
      root = createRoot(host); root.render(createElement(ModernApp));
      await until(() => host.textContent?.includes('root owner') === true && releaseWallet !== undefined);
      const input = host.querySelector('input[aria-label="Todo text"]') as HTMLInputElement;
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, 'retire me');
      input.dispatchEvent(new Event('input', { bubbles: true })); await tick();
      (host.querySelector('button[title="Add"]') as HTMLButtonElement).click();
      await until(() => localStorage.getItem('talkstream_token') === null);
      check(order.indexOf('abort') >= 0 && order.indexOf('abort') < order.indexOf('clear'), `${mode} cleared identity before aborting manual work`);
      check((await loadTodos(userId)).length === 1, `${mode} retained pending mutation`);
      check(!host.textContent?.includes('root owner'), `${mode} retained private UI`);
      if (mode === 'logout') releaseWallet!(new Response(JSON.stringify({ balance_paise: 99999, recent_transactions: [] })));
      await tick(); root.unmount(); root = null; await tick();
      IDBObjectStore.prototype.put = oldPut; IDBTransaction.prototype.abort = oldAbort; Storage.prototype.removeItem = oldRemove;
      checks.push(`modern app ${mode} aborts before identity clear`);
    }
    return checks;
  } finally {
    root?.unmount(); host.remove(); window.fetch = oldFetch;
    IDBObjectStore.prototype.put = oldPut; IDBTransaction.prototype.abort = oldAbort; Storage.prototype.removeItem = oldRemove;
    if (oldToken === null) localStorage.removeItem('talkstream_token'); else localStorage.setItem('talkstream_token', oldToken);
    if (oldEmail === null) localStorage.removeItem('talkstream_email'); else localStorage.setItem('talkstream_email', oldEmail);
    history.replaceState(null, '', oldUrl);
    await new Promise<void>((resolve, reject) => { const r = indexedDB.deleteDatabase(TODO_DATABASE_NAME); r.onsuccess = () => resolve(); r.onerror = () => reject(r.error); });
  }
}

export async function runTodoOwnerRecoveryChecks(): Promise<string[]> {
  const oldFetch = window.fetch;
  const host = document.createElement('div'); document.body.append(host); const root = createRoot(host);
  const ids = Object.fromEntries(['A','B','C','D'].map(token => [token, crypto.randomUUID()]));
  const calls: Record<string, number> = {};
  let hook: ReturnType<typeof useVoiceSession>, releaseRetry: (() => void) | undefined, expirations = 0;
  const expired = () => { expirations += 1; };
  function Harness({ token }: { token: string }) { hook = useVoiceSession(token, undefined, expired); return null; }
  const tick = () => new Promise(resolve => setTimeout(resolve, 0));
  const check = (value: unknown, message: string) => { if (!value) throw new Error(message); };
  async function until(predicate: () => boolean) {
    const end = performance.now()+3000;
    while (!predicate()) { if (performance.now()>end) throw new Error('Timed out waiting for owner recovery'); await tick(); }
  }
  const render = async (token: string) => { root.render(createElement(Harness, { token })); await tick(); await tick(); };
  try {
    window.fetch = (async (_url: string, options?: RequestInit) => {
      const token = new Headers(options?.headers).get('Authorization')!.slice(7);
      calls[token] = (calls[token] ?? 0)+1;
      if (token !== 'B' && calls[token] === 1) return new Response('{}',{status:503});
      if (token === 'C') await new Promise<void>(resolve => { releaseRetry = resolve; });
      if (token === 'D') return new Response('{}',{status:401});
      return new Response(JSON.stringify({id:ids[token]}));
    }) as typeof fetch;
    await executeTodoTool({userId:ids.B,callId:'b-seed',name:'todo_add',arguments:{text:'B stays private'}});
    await render('A'); await until(() => Boolean(hook!.state.todoError));
    check(await hook!.runTodoTool('todo_add',{text:'recovered A'}), 'Recovered owner lookup did not retry');
    await until(() => hook!.state.todos.some(todo => todo.text === 'recovered A'));
    check(calls.A === 2 && (await loadTodos(ids.A)).length === 1, 'Retry did not resolve authenticated owner exactly once');
    await render('C'); await until(() => Boolean(hook!.state.todoError));
    const pending = hook!.runTodoTool('todo_add',{text:'retired C'});
    await until(() => releaseRetry !== undefined);
    await render('B'); await until(() => hook!.state.todos.some(todo => todo.text === 'B stays private'));
    releaseRetry!(); releaseRetry=undefined;
    check(await pending === false && (await loadTodos(ids.C)).length === 0, 'Retired owner retry mutated old account');
    check(hook!.state.todos.length === 1 && hook!.state.todos[0].text === 'B stays private', 'Retired retry replaced current account state');
    await render('D'); await until(() => Boolean(hook!.state.todoError));
    check(await hook!.runTodoTool('todo_add',{text:'unauthorized'}) === false && expirations === 1 && (await loadTodos(ids.D)).length === 0, 'Unauthorized retry retained identity or executed');
    return ['manual retry recovers initial503','account switch retires pending owner retry','owner retry401 expires without mutation'];
  } finally {
    root.unmount(); releaseRetry?.(); host.remove(); window.fetch=oldFetch;
    await new Promise<void>((resolve,reject) => { const r=indexedDB.deleteDatabase(TODO_DATABASE_NAME);r.onsuccess=()=>resolve();r.onerror=()=>reject(r.error); });
  }
}

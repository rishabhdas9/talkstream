import { VoiceClient } from '../src/voice/client';
import { MicrophoneCapture } from '../src/audio/capture';
import { createElement } from 'react';
import { createRoot } from 'react-dom/client';
import { useVoiceSession } from '../src/ui/hooks/useVoiceSession';
import { App as LegacyApp } from '../src/legacy/App';
import { parseServerEvent } from '../src/voice/protocol';

// Run through Vite in a real browser; replace only device and network boundaries.
export async function runVoiceLifecycleChecks(): Promise<string[]> {
  const originalFetch = window.fetch;
  const originalSocket = window.WebSocket;
  const checks: string[] = [];
  let events: string[] = [];
  let releaseMic: (() => void) | undefined;
  let releaseTicket: (() => void) | undefined;
  let mode = 'success';
  let active = false;
  const callbacks = new Proxy({}, { get: () => () => undefined });
  const check = (value: unknown, message: string) => { if (!value) throw new Error(mode + ': ' + message + ': ' + events.join(',')); };
  const tick = () => new Promise(resolve => setTimeout(resolve, 0));
  class Socket {
    static OPEN = 1;
    static CONNECTING = 0;
    readyState = 0;
    onopen: (() => void) | null = null;
    onclose: ((event: { code: number }) => void) | null = null;
    constructor(url: string) {
      events.push('socket');
      check(url.includes('ticket=short-ticket') && !url.includes('persistent-jwt'), 'JWT leaked in socket URL');
      queueMicrotask(() => {
        if (this.readyState !== 0 || mode === 'pending-socket') return;
        if (mode === 'socket-failure') this.close(4401);
        else { this.readyState = 1; this.onopen?.(); }
      });
    }
    send() { this.close(); }
    close(code = 1000) { this.readyState = 3; queueMicrotask(() => this.onclose?.({ code })); }
  }
  function client() {
    const result = new VoiceClient('persistent-jwt', callbacks as never);
    Object.assign(result, {
      capture: {
        async start() {
          events.push('mic');
          if (mode === 'denied') throw new Error('Permission denied');
          if (mode === 'pending-mic') await new Promise<void>(resolve => { releaseMic = resolve; });
          active = true;
        },
        async stop() { events.push('release'); active = false; },
      },
      playback: { invalidate() {}, async close() {} },
    });
    return result;
  }
  try {
    window.WebSocket = Socket as never;
    window.fetch = (async () => {
      events.push('ticket');
      if (mode === 'pending-ticket') await new Promise<void>(resolve => { releaseTicket = resolve; });
      return new Response(JSON.stringify(mode === 'ticket-failure' ? { detail: 'Unavailable' } : { ticket: 'short-ticket' }), { status: mode === 'ticket-failure' ? 503 : 200 });
    }) as typeof fetch;
    for (const scenario of ['denied', 'pending-mic', 'pending-ticket', 'ticket-failure', 'pending-socket', 'socket-failure', 'success']) {
      mode = scenario;
      events = [];
      active = false;
      const voice = client();
      const start = voice.start().then(() => null, error => error);
      if (scenario === 'success') void voice.start();
      await tick();
      if (scenario === 'pending-mic') { await voice.stop(); releaseMic?.(); }
      if (scenario === 'pending-ticket') { await voice.stop(); releaseTicket?.(); }
      if (scenario === 'pending-socket') {
        for (let attempt = 0; attempt < 100 && !events.includes('socket'); attempt++) await tick();
        check(events.includes('socket'), 'Socket never started');
        await voice.stop();
      }
      const error = await start;
      if (scenario === 'success') {
        check(events.slice(0, 3).join(',') === 'mic,ticket,socket', 'Incorrect admission order');
        check(events.filter(event => event === 'socket').length === 1, 'Duplicate start opened another connection');
        await voice.stop();
      } else if (scenario === 'pending-mic') check(!events.includes('ticket') && !events.includes('socket'), 'Cancelled permission opened network');
      else if (scenario === 'pending-ticket') check(!events.includes('socket'), 'Cancelled ticket opened socket');
      else {
        check(error instanceof Error, 'Failure did not propagate');
        if (scenario === 'denied') check(!events.includes('ticket') && !events.includes('socket'), 'Denied permission opened network');
        if (scenario === 'ticket-failure') check(!events.includes('socket'), 'Failed ticket opened socket');
      }
      check(!active, 'Microphone leaked');
      checks.push(scenario);
    }
    return checks;
  } finally { window.fetch = originalFetch; window.WebSocket = originalSocket; }
}

export async function runRealCaptureCancellationCheck(): Promise<string> {
  const originalMedia = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
  const originalFetch = window.fetch;
  const source = new AudioContext();
  const stream = source.createMediaStreamDestination().stream;
  let permit: ((stream: MediaStream) => void) | undefined;
  let requests = 0;
  navigator.mediaDevices.getUserMedia = () => new Promise(resolve => { permit = resolve; });
  window.fetch = (async () => { requests++; throw new Error('Unexpected API call'); }) as typeof fetch;
  try {
    const callbacks = new Proxy({}, { get: () => () => undefined });
    const voice = new VoiceClient('unused-jwt', callbacks as never);
    const started = voice.start();
    await voice.stop();
    permit?.(stream);
    await started;
    if (requests || stream.getTracks().some(track => track.readyState !== 'ended')) {
      throw new Error('Late microphone permission leaked a track or opened the API');
    }
    return 'real capture releases a late-granted track after stop';
  } finally {
    navigator.mediaDevices.getUserMedia = originalMedia;
    window.fetch = originalFetch;
    stream.getTracks().forEach(track => track.stop());
    await source.close();
  }
}

export async function runAudioGenerationChecks(): Promise<string[]> {
  const oldContext = window.AudioContext;
  const oldSocket = window.WebSocket;
  const oldFetch = window.fetch;
  const checks: string[] = [];
  let releaseResume: () => void = () => {};
  let sent: Record<string, unknown>[] = [];
  let scheduled: number[] = [];
  let effects: string[] = [];
  let latestSocket: Socket;
  class Context {
    state = 'suspended'; currentTime = 0; destination = {};
    async resume() { await new Promise<void>(resolve => { releaseResume = resolve; }); this.state = 'running'; }
    async close() { this.state = 'closed'; }
    createGain() { return { connect() {}, gain: { value: 1, cancelScheduledValues() {}, setValueAtTime() {}, linearRampToValueAtTime() {} } }; }
    createBuffer(_channels: number, samples: number, rate: number) {
      const data = new Float32Array(samples);
      return { duration: samples / rate, getChannelData: () => data };
    }
    async decodeAudioData() { return this.createBuffer(1, 240, 24000); }
    createBufferSource() {
      const source = { buffer: null as AudioBuffer | null, onended: null as (() => void) | null,
        connect() {}, start() { scheduled.push(source.buffer!.getChannelData(0)[0] * 32768); },
        stop() { queueMicrotask(() => source.onended?.()); } };
      return source;
    }
  }
  class Socket {
    static OPEN = 1; static CONNECTING = 0; readyState = 0;
    onopen: (() => void) | null = null;
    onclose: ((event: { code: number }) => void) | null = null;
    onmessage: ((event: { data: string | ArrayBuffer }) => void) | null = null;
    constructor() { latestSocket = this; queueMicrotask(() => { this.readyState = 1; this.onopen?.(); }); }
    send(raw: string) { const value = JSON.parse(raw); sent.push(value); if (value.type === 'end_call') this.close(); }
    close(code = 1000) { this.readyState = 3; queueMicrotask(() => this.onclose?.({ code })); }
    control(value: unknown) { this.onmessage?.({ data: JSON.stringify(value) }); }
  }
  function binary(turn: string, generation: string, sequence = 0, sample = 1): ArrayBuffer {
    const json = new TextEncoder().encode(JSON.stringify({ turn_id: turn, generation_id: generation,
      sequence, codec: 'pcm_s16le', sample_rate_hz: 24000, kind: 'answer' }));
    const result = new Uint8Array(9 + json.length + 2); result.set([84,76,75,83,2]);
    new DataView(result.buffer).setUint32(5, json.length, false); result.set(json, 9);
    new DataView(result.buffer).setInt16(9 + json.length, sample, true); return result.buffer;
  }
  const tick = () => new Promise(resolve => setTimeout(resolve, 0));
  const check = (value: unknown, message: string) => { if (!value) throw new Error(message); };
  function begin(turn: string, generation: string) {
    latestSocket.control({ type: 'final_transcript', turn_id: turn, generation_id: generation, text: 'Hi' });
    latestSocket.control({ type: 'audio_start', turn_id: turn, generation_id: generation,
      last_speech_capture_seq: 0, last_speech_capture_time_ms: 0 });
  }
  try {
    window.AudioContext = Context as never; window.WebSocket = Socket as never;
    window.fetch = (async () => new Response(JSON.stringify({ ticket: 'fixture' }))) as typeof fetch;
    for (const scenario of ['interrupt', 'replacement', 'call_end', 'logout', 'cue_interrupt', 'cue_logout', 'cue_answer', 'cue_success']) {
      sent = []; scheduled = []; effects = [];
      const callbacks = new Proxy({}, { get: (_target, key) => () => { effects.push(String(key)); } });
      const client = new VoiceClient('unused', callbacks as never);
      Object.assign(client, { capture: { async start() {}, async stop() {} } });
      await client.start();
      latestSocket!.control({ type: 'ready', capabilities: ['audio_frames_v2', 'response_cues_v1'], tts_sample_rate_hz: 24000 });
      if (scenario.startsWith('cue_')) {
        // Wait for the optional bundled asset; this is not part of answer readiness.
        await (client as unknown as { playback: { preloadFeedback(key: string, url: string): Promise<void> } }).playback.preloadFeedback('neutral_ack', '/audio/response-cues/neutral-ack.wav');
        await tick();
        latestSocket!.control({ type: 'final_transcript', turn_id: 'call:1', generation_id: 'g-1', text: 'Hi' });
        latestSocket!.control({ type: 'response_cue', turn_id: 'call:1', generation_id: 'g-1', cue_id: 'cue-1',
          cue_key: 'neutral_ack', language_code: 'hi-IN', delay_ms: 0, last_speech_capture_time_ms: 0 });
      } else {
        begin('call:1', 'g-1');
        latestSocket!.onmessage?.({ data: binary('call:1', 'g-1') });
      }
      await tick();
      const resumeFirst = releaseResume;
      if (scenario === 'replacement') {
        begin('call:2', 'g-2');
        latestSocket!.onmessage?.({ data: binary('call:2', 'g-2', 0, 2) });
      } else if (scenario === 'interrupt' || scenario === 'cue_interrupt') {
        latestSocket!.control({ type: 'interrupt', turn_id: 'call:1', generation_id: 'g-1', reason: 'barge_in' });
        latestSocket!.onmessage?.({ data: binary('call:1', 'g-1', 1) });
      } else if (scenario === 'call_end') latestSocket!.control({ type: 'call_ended', reason: 'user' });
      else if (scenario === 'cue_answer') {
        latestSocket!.control({ type: 'audio_start', turn_id: 'call:1', generation_id: 'g-1',
          last_speech_capture_seq: 0, last_speech_capture_time_ms: 0 });
        latestSocket!.onmessage?.({ data: binary('call:1', 'g-1', 0, 2) });
        await tick();
      } else if (scenario !== 'cue_success') {
        const stopped = client.stop();
        check(await Promise.race([stopped.then(() => true), new Promise(resolve => setTimeout(() => resolve(false), 200))]), 'stop waited for browser resume');
      }
      resumeFirst(); await tick(); await tick();
      if (scenario === 'cue_answer') {
        releaseResume(); await tick(); await tick();
        check(scheduled.length === 1 && scheduled[0] === 2, 'cancelled cue played over the answer');
        check(!sent.some(value => value.type === 'response_cue_started'), 'cancelled cue acknowledgement');
      } else if (scenario === 'cue_success') {
        check(scheduled.length === 1, 'current cue failed to play: ' + JSON.stringify({ scheduled, sent }));
        check(sent.some(value => value.type === 'response_cue_started' && value.generation_id === 'g-1'), 'current cue acknowledgement missing');
      } else if (scenario === 'replacement') {
        // The replacement requests its own resume after the cancelled item releases the chain.
        releaseResume(); await tick(); await tick();
        check(scheduled.length === 1 && scheduled[0] === 2, 'old frame scheduled or was relabelled as replacement');
        check(sent.filter(value => value.type === 'playback_started').every(value => value.turn_id === 'call:2' && value.generation_id === 'g-2'), 'stale playback acknowledgement');
      } else {
        check(scheduled.length === 0 && !sent.some(value => value.type === 'playback_started' || value.type === 'response_cue_started'), 'stale audio or acknowledgement after invalidation');
        if (scenario.includes('interrupt')) check(sent.some(value => value.type === 'interrupt_ack' && value.generation_id === 'g-1'), 'generation-scoped interrupt acknowledgement missing');
      }
      await client.stop();
      const effectCount = effects.length;
      latestSocket!.control({ type: 'agent_text', turn_id: 'call:1', generation_id: 'g-1', text: 'late' });
      latestSocket!.control({ type: 'tool_request', call_id: 'late', name: 'todo_add', arguments: { text: 'must not execute' } });
      latestSocket!.onmessage?.({ data: binary('call:1', 'g-1', 2) });
      await tick(); check(effects.length === effectCount, 'late callbacks mutated a retired client');
      checks.push(scenario);
    }
    return checks;
  } finally { window.AudioContext = oldContext; window.WebSocket = oldSocket; window.fetch = oldFetch; }
}

export async function runTerminalSettlementChecks(target = 'all'): Promise<string[]> {
  const oldFetch = window.fetch;
  const oldSocket = window.WebSocket;
  const oldStart = MicrophoneCapture.prototype.start;
  const oldStop = MicrophoneCapture.prototype.stop;
  const oldMedia = navigator.mediaDevices.getUserMedia;
  const oldToken = localStorage.getItem('talkstream_token');
  const host = document.createElement('div'); document.body.append(host);
  const tick = () => new Promise(resolve => setTimeout(resolve, 0));
  const check = (value: unknown, message: string) => { if (!value) throw new Error(message); };
  const finalBill = { type: 'billing', session_id: 'call', seconds: 11, cost_paise: 37,
    session_cost_paise: 37, charged_paise: 37, balance_paise: 463, low_balance: false,
    warning: null, final: true, terminated_reason: null };
  let latestSocket: Socket;
  let ended = false;
  class Socket {
    static OPEN = 1; static CONNECTING = 0; readyState = 0;
    onopen: (() => void) | null = null;
    onclose: ((event: { code: number }) => void) | null = null;
    onmessage: ((event: { data: string }) => void) | null = null;
    constructor() { latestSocket = this; queueMicrotask(() => { this.readyState = 1; this.onopen?.(); }); }
    send(raw: string) { if (JSON.parse(raw).type === 'end_call') ended = true; }
    close(code = 1000) { this.readyState = 3; queueMicrotask(() => this.onclose?.({ code })); }
    control(value: unknown) { this.onmessage?.({ data: JSON.stringify(value) }); }
    settle() { this.control(finalBill); this.control({ type: 'call_ended', reason: 'user' }); this.close(); }
  }
  let root: ReturnType<typeof createRoot> | null = null;
  try {
    let rejected = false;
    try { parseServerEvent(JSON.stringify({ type: 'call_ended', reason: 'error', message: {} })); }
    catch { rejected = true; }
    check(rejected, 'terminal message parser accepted a non-string error');
    window.WebSocket = Socket as never;
    window.fetch = (async (url: string) => new Response(JSON.stringify(String(url).endsWith('/auth/me') ? { id: 'fixture-user' } : String(url).includes('ticket')
      ? { ticket: 'fixture' } : { balance_paise: 500, transactions: [] }))) as typeof fetch;
    MicrophoneCapture.prototype.start = async () => {};
    MicrophoneCapture.prototype.stop = async () => {};
    navigator.mediaDevices.getUserMedia = async () => ({ getTracks: () => [] }) as unknown as MediaStream;
    const effects: string[] = [];
    const callbacks = new Proxy({}, { get: (_target, key) => (value: unknown) => { effects.push(`${String(key)}:${JSON.stringify(value)}`); } });
    const checks: string[] = [];
    if (target === 'all' || target === 'client') {
      const client = new VoiceClient('fixture', callbacks as never);
      await client.start();
      const stopping = client.stop();
      check(ended, 'graceful stop did not request end_call');
      latestSocket!.control({ type: 'agent_text', turn_id: 'call:1', generation_id: 'g-1', text: 'late' });
      latestSocket!.control({ type: 'tool_request', call_id: 'late', name: 'todo_add', arguments: { text: 'must not execute' } });
      latestSocket!.settle();
      const settlement = await stopping;
      check(JSON.stringify(settlement) === JSON.stringify(finalBill), 'graceful stop lost authoritative final billing');
      check(effects.length === 0, 'stopping client ran late callbacks/actions');
      checks.push('client final settlement');
    }

    if (target === 'all' || target === 'fatal') {
      const failed = new VoiceClient('fixture', callbacks as never);
      await failed.start();
      latestSocket!.control(finalBill);
      latestSocket!.control({ type: 'call_ended', reason: 'error', message: 'Voice session failed' });
      latestSocket!.control({ type: 'error', code: 'voice_session_error', message: 'Voice session failed' });
      latestSocket!.close(); await failed.stop();
      check(effects.filter(value => value === 'onError:"Voice session failed"').length === 1, 'fatal call lost or duplicated visible error');
      checks.push('visible fatal termination');
    }

    let hook: ReturnType<typeof useVoiceSession>;
    let walletChanges: number[] = [];
    function Harness({ token }: { token: string }) {
      hook = useVoiceSession(token, balance => walletChanges.push(balance));
      return null;
    }
    for (const mode of ['hangup', 'logout', 'replacement', 'fatal']) {
      if (target !== 'all' && target !== 'modern') continue;
      ended = false; walletChanges = [];
      root = createRoot(host); root.render(createElement(Harness, { token: 'fixture' })); await tick(); await tick();
      await hook!.start();
      latestSocket!.control({ type: 'ready', session_id: 'call', balance_paise: 500, capabilities: ['audio_frames_v2'] }); await tick();
      const oldCall = latestSocket!;
      if (mode === 'fatal') {
        oldCall.control(finalBill);
        oldCall.control({ type: 'call_ended', reason: 'error', message: 'Voice session failed' });
        oldCall.control({ type: 'error', message: 'Voice session failed' }); oldCall.close();
        await tick(); await tick();
        check(hook!.state.error === 'Voice session failed' && hook!.state.sessionCostPaise === 37, 'modern fatal termination lost its error or final bill');
        root.unmount(); root = null; await tick(); checks.push('modern fatal'); continue;
      }
      // The object form makes the synchronous logout intent explicit at the UI boundary.
      const stopped = mode === 'logout'
        ? hook!.stop({ discardFinalBilling: true }) : hook!.stop();
      const next = mode === 'replacement' ? hook!.start() : Promise.resolve();
      if (mode === 'logout') { root.render(createElement(Harness, { token: '' })); await tick(); }
      oldCall.settle(); await stopped; await next; await tick(); await tick();
      oldCall.control(finalBill); await tick();
      if (mode === 'hangup') {
        check(hook!.state.sessionCostPaise === 37 && hook!.state.balancePaise === 463 && hook!.state.elapsedSeconds === 11, `modern hangup omitted final bill: ${JSON.stringify(hook!.state)}`);
        check(walletChanges.length === 1 && walletChanges[0] === 463, 'modern wallet did not receive final bill exactly once');
      } else {
        check(walletChanges.length === 0 && hook!.state.sessionCostPaise === 0, 'retired modern call mutated logout/replacement state');
      }
      if (mode === 'replacement') { const ending = hook!.stop(); latestSocket!.settle(); await ending; }
      root.unmount(); root = null; await tick(); checks.push(`modern ${mode}`);
    }

    for (const mode of ['hangup', 'logout', 'fatal']) {
      if (target !== 'all' && target !== 'legacy') continue;
      ended = false; localStorage.setItem('talkstream_token', 'fixture');
      root = createRoot(host); root.render(createElement(LegacyApp)); await tick(); await tick();
      const button = (label: string) => Array.from(host.querySelectorAll('button')).find(value => value.textContent === label)!;
      button('Start call').click(); await tick(); await tick();
      if (mode === 'fatal') {
        latestSocket!.control(finalBill);
        latestSocket!.control({ type: 'call_ended', reason: 'error', message: 'Voice session failed' });
        latestSocket!.control({ type: 'error', message: 'Voice session failed' });
        latestSocket!.close(); await tick(); await tick();
        check(host.textContent?.includes('Voice session failed'), 'legacy fatal termination is invisible');
      } else {
        button('Stop call').click(); await tick();
        latestSocket!.settle(); await tick(); await tick();
        if (mode === 'logout') {
          button('Log out').click(); await tick();
          latestSocket!.control(finalBill); await tick();
        }
        if (mode === 'hangup') check(host.textContent?.includes('Wallet balance: 463 paise') && host.textContent?.includes('Current session cost: 37 paise'), 'legacy hangup omitted final bill');
        else check(host.textContent?.includes('Request OTP') && !host.textContent?.includes('463'), 'late billing mutated logged-out legacy UI');
      }
      root.unmount(); root = null; await tick(); checks.push(`legacy ${mode}`);
    }
    return checks;
  } finally {
    root?.unmount(); host.remove(); window.fetch = oldFetch; window.WebSocket = oldSocket;
    MicrophoneCapture.prototype.start = oldStart; MicrophoneCapture.prototype.stop = oldStop;
    navigator.mediaDevices.getUserMedia = oldMedia;
    if (oldToken == null) localStorage.removeItem('talkstream_token'); else localStorage.setItem('talkstream_token', oldToken);
  }
}

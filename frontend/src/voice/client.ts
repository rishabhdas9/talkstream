import { getCurrentUser, voiceWebSocketUrl, requestVoiceTicket } from "../api/auth";
import { errorForVoiceClose, isAuthenticationExpiredError } from "../api/errors";
import { MicrophoneCapture } from "../audio/capture";
import { PcmPlaybackQueue } from "../audio/playback";
import { executeTodoTool, loadTodos, readTodoReceipt } from "../store/todoDb";
import type { Todo } from "../store/todos";
import {
  encodeCaptureFrame,
  decodeOutputAudio,
  type AudioFrame,
  parseServerEvent,
  type ServerEvent,
  type SearchSource,
  type TurnMetricsEvent,
} from "./protocol";

export type BillingEvent = Extract<ServerEvent, { type: "billing" }>;

export type VoiceClientCallbacks = {
  onState: (state: string) => void;
  onTranscript: (text: string, languageCode: string | null | undefined, turnId: string) => void;
  onAgentText: (text: string, turnId: string) => void;
  onMetrics: (metrics: TurnMetricsEvent) => void;
  onInterrupted: (message: string) => void;
  onTodosChanged: (todos: Todo[]) => void;
  onBilling: (event: BillingEvent) => void;
  onError: (message: string) => void;
  onAuthenticationExpired?: () => void;
  // Optional structured hooks used by the redesigned UI; the legacy UI
  // relies only on the string-based callbacks above.
  onReady?: (event: Extract<ServerEvent, { type: "ready" }>) => void;
  onAgentState?: (event: Extract<ServerEvent, { type: "agent_state" }>) => void;
  onCallEnded?: (event: Extract<ServerEvent, { type: "call_ended" }>) => void;
  onSearchResults?: (results: SearchSource[], turnId: string) => void;
};

export class VoiceClient {
  private socket: WebSocket | null = null;
  private readonly capture = new MicrophoneCapture();
  private readonly playback = new PcmPlaybackQueue();
  private captureSeq = 0;
  private readonly audioStarts = new Map<string, Extract<ServerEvent, { type: "audio_start" }>>();
  private activeGeneration: { turnId: string; generationId: string } | null = null;
  private currentThinkingTurnId: string | null = null;
  private playbackReportedForTurn: string | null = null;
  private playbackChain: Promise<void> = Promise.resolve();
  private acceptingAudio = false;
  private playedAudioMsByTurn = new Map<string, number>();
  private playbackEndByTurn = new Map<string, number>();
  private playbackStartByTurn = new Map<string, number>();
  private interruptedTurns = new Set<string>();
  private finishedTurns = new Set<string>();
  private interruptedAtMs = new Map<string, number>();
  private audioEpoch = 0;
  private muted = false;
  private serverEnded = false;
  private callEndedPromise: Promise<void> = Promise.resolve();
  private resolveCallEnded: (() => void) | null = null;
  private stopPromise: Promise<void> | null = null;
  private startPromise: Promise<void> | null = null;
  private stopped = false;
  private finalBilling: BillingEvent | null = null;
  private toolController = new AbortController();
  private userIdPromise: Promise<string> | null = null;

  constructor(
    private readonly token: string,
    private readonly callbacks: VoiceClientCallbacks,
  ) {}

  async start(): Promise<void> {
    if (this.stopped) return;
    this.startPromise ??= this.performStart();
    await this.startPromise;
  }

  private async performStart(): Promise<void> {
    try {
      await this.capture.start((pcm, captureTimeMs) => {
        const socket = this.socket;
        if (this.muted || this.stopped || socket?.readyState !== WebSocket.OPEN) return;
        socket.send(encodeCaptureFrame(this.captureSeq, captureTimeMs, pcm));
        this.captureSeq += 1;
      });
      if (this.stopped) {
        await this.capture.stop();
        return;
      }
      const ticket = await requestVoiceTicket(this.token);
      if (this.stopped) {
        await this.capture.stop();
        return;
      }
      await this.connect(ticket);
    } catch (error) {
      await this.stop();
      throw error;
    }
  }

  private async connect(ticket: string): Promise<void> {
    const socket = new WebSocket(voiceWebSocketUrl(ticket));
    let resolveConnection: (() => void) | null = null;
    let rejectConnection: ((reason: Error) => void) | null = null;
    const connected = new Promise<void>((resolve, reject) => {
      resolveConnection = resolve;
      rejectConnection = reject;
    });
    this.serverEnded = false;
    this.callEndedPromise = new Promise((resolve) => {
      this.resolveCallEnded = resolve;
    });
    this.acceptingAudio = true;
    socket.binaryType = "arraybuffer";
    this.socket = socket;
    socket.onmessage = (event) => {
      if (this.socket !== socket) return;
      try { this.handleMessage(event); }
      catch (error) {
        if (this.stopped) return;
        this.callbacks.onError(String(error));
        void this.stop();
      }
    };
    socket.onclose = (event) => {
      if (rejectConnection != null) {
        rejectConnection(errorForVoiceClose(event.code));
        rejectConnection = null;
      }
      if (this.socket === socket) this.socket = null;
      this.resolveCallEnded?.();
      if (this.stopped) return;
      if (event.code === 4401) {
        this.callbacks.onAuthenticationExpired?.();
      } else if (!this.serverEnded) {
        this.callbacks.onState(
          event.code === 4403 ? "ended - balance exhausted" : "disconnected",
        );
      }
      // A remote close must release the microphone and AudioContexts even
      // when the UI never calls stop(), for example after a pipeline error.
      void this.stop();
    };
    socket.onopen = () => {
      resolveConnection?.();
      resolveConnection = null;
      rejectConnection = null;
    };
    // The close event carries the useful application code; browser error
    // events intentionally do not expose handshake details.
    socket.onerror = () => undefined;
    await connected;
    if (this.stopped || this.socket !== socket) {
      // stop() ran while the socket was connecting; do not become a zombie
      // session that streams mic audio with playback disabled.
      socket.close(1000);
      return;
    }
  }

  setMuted(muted: boolean): void {
    this.muted = muted;
  }

  isMuted(): boolean {
    return this.muted;
  }

  async stop(): Promise<BillingEvent | null> {
    this.stopped = true;
    this.invalidateAudio();
    this.userIdPromise = null;
    if (this.stopPromise == null) {
      this.stopPromise = this.performStop();
    }
    await this.stopPromise;
    return this.finalBilling;
  }

  private async performStop(): Promise<void> {
    this.acceptingAudio = false;
    const localCleanup = Promise.allSettled([this.capture.stop(), this.playback.close()]);
    const socket = this.socket;
    let timer: number | undefined;
    try {
      if (socket?.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify({ type: "end_call" }));
        await Promise.race([
          this.callEndedPromise,
          new Promise<void>((resolve) => { timer = window.setTimeout(resolve, 1500); }),
        ]);
      }
    } catch {
      // Local resources must close even when the transport has already failed.
    } finally {
      if (timer != null) window.clearTimeout(timer);
      if (socket?.readyState === WebSocket.OPEN || socket?.readyState === WebSocket.CONNECTING) {
        try { socket.close(); } catch { /* Already closed by the remote endpoint. */ }
      }
      if (this.socket === socket) this.socket = null;
      await localCleanup;
      await this.playbackChain;
    }
  }

  /** How much of a turn's answer audio has actually played, for pacing its text. */
  speechProgress(turnId: string): { playedMs: number; totalMs: number; finished: boolean } {
    const totalMs = this.playedAudioMsByTurn.get(turnId) ?? 0;
    const frozen = this.interruptedAtMs.get(turnId);
    const end = this.playbackEndByTurn.get(turnId);
    const playedMs = frozen ?? (end == null ? 0 : Math.max(0, totalMs - Math.max(0, end - performance.now())));
    return { playedMs, totalMs, finished: this.finishedTurns.has(turnId) };
  }

  private invalidateAudio(): void {
    this.retireToolRequests();
    this.audioEpoch += 1;
    this.activeGeneration = null;
    this.currentThinkingTurnId = null;
    this.playback.invalidate();
  }

  private isCurrent(turnId: string, generationId: string): boolean {
    return this.acceptingAudio && !this.stopped && !this.interruptedTurns.has(turnId) &&
      this.activeGeneration?.turnId === turnId && this.activeGeneration.generationId === generationId &&
      this.playback.isCurrent(turnId, generationId);
  }

  private handleMessage(event: MessageEvent<string | ArrayBuffer>): void {
    if (event.data instanceof ArrayBuffer) {
      if (this.stopped || !this.acceptingAudio) return;
      const frame = decodeOutputAudio(event.data);
      const marker = this.audioStarts.get(frame.header.generation_id);
      if (!marker || frame.header.kind !== "answer" || marker.turn_id !== frame.header.turn_id ||
          !this.isCurrent(frame.header.turn_id, frame.header.generation_id)) return;
      const epoch = this.audioEpoch;
      this.playbackChain = this.playbackChain
        .then(() => this.handleAudio(frame, marker, epoch))
        .catch((error: unknown) => {
          if (this.stopped) return;
          this.callbacks.onError(String(error));
          void this.stop();
        });
      return;
    }
    const message = parseServerEvent(event.data);
    if (message.type === "billing" && message.final && !this.serverEnded) this.finalBilling = message;
    if (this.stopped || !this.acceptingAudio) {
      // Settlement survives graceful hang-up; retired callers decide whether to apply it.
      if (message.type === "call_ended") {
        this.serverEnded = true;
        this.resolveCallEnded?.();
      }
      return;
    }
    switch (message.type) {
      case "ready":
        if (!message.capabilities?.includes("audio_frames_v2")) throw new Error("Unsupported voice audio protocol");
        if (message.capabilities?.includes("response_cues_v1")) {
          void this.playback.preloadFeedback(
            "neutral_ack",
            "/audio/response-cues/neutral-ack.wav",
          );
        }
        this.callbacks.onReady?.(message);
        this.callbacks.onState("listening");
        break;
      case "agent_state":
        this.callbacks.onAgentState?.(message);
        this.callbacks.onState(
          message.detail ? `${message.state} - ${message.detail}` : message.state,
        );
        break;
      case "billing":
        this.callbacks.onBilling(message);
        break;
      case "final_transcript":
        if (!this.playback.beginGeneration(message.turn_id, message.generation_id)) break;
        if (this.activeGeneration?.generationId !== message.generation_id || this.activeGeneration?.turnId !== message.turn_id) this.retireToolRequests();
        this.activeGeneration = { turnId: message.turn_id, generationId: message.generation_id };
        this.currentThinkingTurnId = message.turn_id;
        this.callbacks.onTranscript(message.text, message.language_code, message.turn_id);
        break;
      case "agent_text":
        if (!this.isCurrent(message.turn_id, message.generation_id)) break;
        this.callbacks.onAgentText(message.text, message.turn_id);
        break;
      case "search_results":
        if (!this.isCurrent(message.turn_id, message.generation_id)) break;
        this.callbacks.onSearchResults?.(message.results, message.turn_id);
        break;
      case "audio_start":
        if (!this.isCurrent(message.turn_id, message.generation_id)) break;
        this.playback.cancelAllFeedback();
        if (this.currentThinkingTurnId === message.turn_id) this.currentThinkingTurnId = null;
        this.audioStarts.set(message.generation_id, Object.freeze({ ...message }));
        break;
      case "response_cue":
        void this.handleResponseCue(message);
        break;
      case "response_cue_cancel":
        if (!this.isCurrent(message.turn_id, message.generation_id)) break;
        this.playback.cancelFeedback(message.turn_id, message.cue_id);
        if (this.currentThinkingTurnId === message.turn_id) this.currentThinkingTurnId = null;
        break;
      case "interrupt":
        this.handleInterrupt(message);
        break;
      case "interrupt_resolved":
        this.callbacks.onInterrupted(
          `Turn ${message.turn_id.split(":").at(-1)} interruption acknowledged in ${
            message.barge_in_stop_ack_ms == null
              ? "n/a"
              : `${message.barge_in_stop_ack_ms.toFixed(1)} ms`
          }`,
        );
        break;
      case "tool_request":
        void this.handleToolRequest(message).catch(() => {
          if (this.stopped) return;
          this.callbacks.onError("Browser todo communication failed.");
          void this.stop();
        });
        break;
      case "tool_receipt_query":
        void this.handleReceiptQuery(message).catch(() => {
          if (this.stopped) return;
          this.callbacks.onError("Browser receipt communication failed.");
          void this.stop();
        });
        break;
      case "turn_metrics":
        this.callbacks.onMetrics(message);
        break;
      case "turn_complete":
        if (this.isCurrent(message.turn_id, message.generation_id)) void this.reportPlaybackFinished(message.turn_id, message.generation_id);
        break;
      case "error":
        if (!("generation_id" in message) || (message.generation_id != null && message.turn_id != null && this.isCurrent(message.turn_id, message.generation_id))) this.invalidateAudio();
        this.callbacks.onError(message.message);
        break;
      case "call_ended":
        this.invalidateAudio();
        this.acceptingAudio = false;
        this.serverEnded = true;
        this.resolveCallEnded?.();
        if (message.message) this.callbacks.onError(message.message);
        this.callbacks.onCallEnded?.(message);
        this.callbacks.onState(
          message.reason === "balance_exhausted"
            ? "ended - balance exhausted"
            : "ended",
        );
        void this.stop();
        break;
      default:
        break;
    }
  }

  private retireToolRequests(): void {
    this.toolController.abort();
    this.toolController = new AbortController();
  }

  private userId(): Promise<string> {
    this.userIdPromise ??= getCurrentUser(this.token).then(user => user.id).catch(error => {
      this.userIdPromise = null;
      throw error;
    });
    return this.userIdPromise;
  }

  private async handleToolRequest(
    message: Extract<ServerEvent, { type: "tool_request" }>,
  ): Promise<void> {
    const socket = this.socket;
    const signal = this.toolController.signal;
    const live = () => !this.stopped && !signal.aborted && this.socket === socket &&
      socket?.readyState === WebSocket.OPEN && this.isCurrent(message.turn_id, message.generation_id);
    if (!live()) return;
    let result: Record<string, unknown>;
    let userId: string;
    try {
      userId = await this.userId();
      if (!live()) return;
      result = await executeTodoTool({ userId, callId: message.call_id, name: message.name,
        arguments: message.arguments, requestHash: message.request_hash, signal });
    } catch (error) {
      if (!live()) return;
      if (isAuthenticationExpiredError(error)) this.callbacks.onAuthenticationExpired?.();
      if (!live()) return;
      this.callbacks.onError("Could not save browser todos. Please try again.");
      if (!live()) return;
      socket?.send(JSON.stringify({ type: "tool_result", call_id: message.call_id,
        result: { ok: false, state: "failed", error: "Browser todo operation was rejected." } }));
      return;
    }
    if (!live()) return;
    socket?.send(JSON.stringify({ type: "tool_result", call_id: message.call_id, result }));
    try {
      const todos = await loadTodos(userId);
      if (live()) this.callbacks.onTodosChanged(todos);
    } catch {
      if (live()) this.callbacks.onError("Todos were saved, but the list could not be refreshed.");
    }
  }

  private async handleReceiptQuery(message: Extract<ServerEvent, { type: "tool_receipt_query" }>): Promise<void> {
    const socket = this.socket, signal = this.toolController.signal;
    const live = () => !this.stopped && !signal.aborted && this.socket === socket && socket?.readyState === WebSocket.OPEN;
    if (!live()) return;
    let result = null;
    try {
      const userId = await this.userId();
      if (!live()) return;
      result = await readTodoReceipt(userId, message.call_id, message.request_hash, signal);
    } catch (error) {
      if (!live()) return;
      if (isAuthenticationExpiredError(error)) this.callbacks.onAuthenticationExpired?.();
      if (!live()) return;
      this.callbacks.onError("Could not read the saved browser action receipt.");
    }
    if (live()) socket?.send(JSON.stringify({ type: "tool_result", call_id: message.request_id, result }));
  }

  private async handleResponseCue(
    message: Extract<ServerEvent, { type: "response_cue" }>,
  ): Promise<void> {
    if (
      !this.isCurrent(message.turn_id, message.generation_id) ||
      this.currentThinkingTurnId !== message.turn_id ||
      this.interruptedTurns.has(message.turn_id)
    )
      return;
    const cueStartPerfMs = await this.playback.playFeedback(
      message.turn_id,
      message.generation_id,
      message.cue_id,
      message.cue_key,
    );
    if (cueStartPerfMs == null || !this.isCurrent(message.turn_id, message.generation_id) || this.socket?.readyState !== WebSocket.OPEN) return;
    this.socket.send(
      JSON.stringify({
        type: "response_cue_started",
        turn_id: message.turn_id,
        generation_id: message.generation_id,
        cue_id: message.cue_id,
        cue_start_perf_ms: cueStartPerfMs,
        feedback_voice_to_voice_ms:
          cueStartPerfMs - message.last_speech_capture_time_ms,
      }),
    );
  }

  private async handleAudio(frame: AudioFrame, marker: Extract<ServerEvent, { type: "audio_start" }>, epoch: number): Promise<void> {
    if (epoch !== this.audioEpoch || !this.isCurrent(frame.header.turn_id, frame.header.generation_id)) return;
    const schedule = await this.playback.enqueue(frame);
    if (schedule == null || epoch !== this.audioEpoch || !this.isCurrent(frame.header.turn_id, frame.header.generation_id)) return;
    if (marker) {
      this.playedAudioMsByTurn.set(
        marker.turn_id,
        (this.playedAudioMsByTurn.get(marker.turn_id) ?? 0) + schedule.durationMs,
      );
      this.playbackEndByTurn.set(marker.turn_id, schedule.playbackEndPerfMs);
      if (!this.playbackStartByTurn.has(marker.turn_id)) {
        this.playbackStartByTurn.set(marker.turn_id, schedule.playbackStartPerfMs);
      }
    }
    if (
      marker &&
      this.playbackReportedForTurn !== marker.turn_id &&
      this.socket?.readyState === WebSocket.OPEN
    ) {
      this.playbackReportedForTurn = marker.turn_id;
      this.socket.send(
        JSON.stringify({
          type: "playback_started",
          turn_id: marker.turn_id,
          generation_id: marker.generation_id,
          last_speech_capture_seq: marker.last_speech_capture_seq,
          audio_received_perf_ms: schedule.receivedPerfMs,
          decode_complete_perf_ms: schedule.decodeCompletePerfMs,
          scheduled_perf_ms: schedule.scheduledPerfMs,
          playback_start_perf_ms: schedule.playbackStartPerfMs,
          e2e_voice_to_voice_ms:
            schedule.playbackStartPerfMs - marker.last_speech_capture_time_ms,
          e2e_anchor_source: marker.speech_anchor_source,
        }),
      );
    }
  }

  private async reportPlaybackFinished(turnId: string, generationId: string): Promise<void> {
    await this.playbackChain;
    await this.playback.whenIdle();
    if (this.isCurrent(turnId, generationId)) this.finishedTurns.add(turnId);
    if (
      this.socket?.readyState !== WebSocket.OPEN ||
      !this.isCurrent(turnId, generationId)
    )
      return;
    this.socket.send(
      JSON.stringify({
        type: "playback_finished",
        turn_id: turnId,
        generation_id: generationId,
        playback_end_perf_ms: this.playbackEndByTurn.get(turnId) ?? performance.now(),
        played_audio_ms: this.playedAudioMsByTurn.get(turnId) ?? 0,
      }),
    );
  }

  private handleInterrupt(message: Extract<ServerEvent, { type: "interrupt" }>): void {
    const turnId = message.turn_id;
    if (!this.isCurrent(turnId, message.generation_id)) return;
    const interruptReceivedPerfMs = performance.now();
    const generatedAudioMs = this.playedAudioMsByTurn.get(turnId) ?? 0;
    const playbackStartPerfMs = this.playbackStartByTurn.get(turnId);
    const playedAudioMs =
      playbackStartPerfMs == null
        ? 0
        : Math.min(
            generatedAudioMs,
            Math.max(0, interruptReceivedPerfMs - playbackStartPerfMs),
          );
    this.interruptedTurns.add(turnId);
    this.interruptedAtMs.set(turnId, playedAudioMs);
    this.invalidateAudio();
    if (this.currentThinkingTurnId === turnId) this.currentThinkingTurnId = null;
    const queueClearedPerfMs = performance.now();
    this.callbacks.onState("interrupted - user started speaking");
    this.callbacks.onInterrupted(
      `Turn ${turnId.split(":").at(-1)} interrupted after ${playedAudioMs.toFixed(1)} ms played`,
    );
    if (this.socket?.readyState === WebSocket.OPEN) {
      this.socket.send(
        JSON.stringify({
          type: "interrupt_ack",
          turn_id: turnId,
          generation_id: message.generation_id,
          interrupt_received_perf_ms: interruptReceivedPerfMs,
          queue_cleared_perf_ms: queueClearedPerfMs,
          audio_queue_cleared: true,
          played_audio_ms: playedAudioMs,
        }),
      );
    }
  }
}

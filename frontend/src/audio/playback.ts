import { FeedbackCueLifecycle } from "./feedback.ts";
import type { AudioFrame } from "../voice/protocol.ts";

export type PlaybackSchedule = {
  receivedPerfMs: number;
  decodeCompletePerfMs: number;
  scheduledPerfMs: number;
  playbackStartPerfMs: number;
  playbackEndPerfMs: number;
  durationMs: number;
};

export class PcmPlaybackQueue {
  private readonly context = new AudioContext();
  private readonly answerGain = this.context.createGain();
  private readonly feedbackGain = this.context.createGain();
  private readonly feedbackLifecycle = new FeedbackCueLifecycle();
  private readonly feedbackBuffers = new Map<string, AudioBuffer>();
  private active: { turnId: string; generationId: string; nextSequence: number } | null = null;
  private readonly retired = new Set<string>();
  private revision = 0;
  private closed = false;
  private cancelResume: () => void = () => {};
  private invalidated = new Promise<void>((resolve) => { this.cancelResume = resolve; });
  private nextStartTime = 0;
  private sources = new Set<AudioBufferSourceNode>();
  private idleWaiters: Array<() => void> = [];
  private feedbackSource: AudioBufferSourceNode | null = null;

  constructor() {
    this.answerGain.connect(this.context.destination);
    this.feedbackGain.gain.value = 0.45;
    this.feedbackGain.connect(this.context.destination);
  }

  beginGeneration(turnId: string, generationId: string): boolean {
    if (this.closed || this.contextIsClosed() || this.retired.has(generationId)) return false;
    if (this.active?.generationId === generationId) return this.active.turnId === turnId;
    this.invalidate();
    this.active = { turnId, generationId, nextSequence: 0 };
    return true;
  }

  isCurrent(turnId: string, generationId: string): boolean {
    return !this.closed && !this.contextIsClosed() &&
      this.active?.turnId === turnId && this.active.generationId === generationId;
  }

  async enqueue(frame: AudioFrame): Promise<PlaybackSchedule | null> {
    const header = { ...frame.header };
    const revision = this.revision;
    if (!this.isCurrent(header.turn_id, header.generation_id) ||
        header.sequence !== this.active?.nextSequence) return null;
    this.active.nextSequence += 1;
    if (header.kind === "answer") this.cancelAllFeedback();
    const cancelled = this.invalidated;
    const resumed = await Promise.race([
      this.context.resume().then(() => true), cancelled.then(() => false),
    ]);
    if (!resumed || revision !== this.revision || !this.isCurrent(header.turn_id, header.generation_id)) return null;
    const receivedPerfMs = performance.now();
    const samples = new DataView(frame.payload);
    const buffer = this.context.createBuffer(1, samples.byteLength / 2, header.sample_rate_hz);
    const channel = buffer.getChannelData(0);
    for (let index = 0; index < channel.length; index += 1) {
      channel[index] = samples.getInt16(index * 2, true) / 32768;
    }
    const decodeCompletePerfMs = performance.now();
    const source = this.context.createBufferSource();
    source.buffer = buffer;
    source.connect(header.kind === "answer" ? this.answerGain : this.feedbackGain);
    const startTime = Math.max(this.context.currentTime + 0.01, this.nextStartTime);
    this.nextStartTime = startTime + buffer.duration;
    if (revision !== this.revision || !this.isCurrent(header.turn_id, header.generation_id)) return null;
    this.sources.add(source);
    source.onended = () => {
      this.sources.delete(source);
      if (this.sources.size === 0) {
        this.idleWaiters.splice(0).forEach((resolve) => resolve());
      }
    };
    source.start(startTime);
    const contextToPerformanceOffset = performance.now() - this.context.currentTime * 1000;
    const scheduledPerfMs = performance.now();
    return {
      receivedPerfMs,
      decodeCompletePerfMs,
      scheduledPerfMs,
      playbackStartPerfMs: contextToPerformanceOffset + startTime * 1000,
      playbackEndPerfMs: contextToPerformanceOffset + (startTime + buffer.duration) * 1000,
      durationMs: buffer.duration * 1000,
    };
  }

  async preloadFeedback(key: string, url: string): Promise<void> {
    if (this.feedbackBuffers.has(key) || this.contextIsClosed()) return;
    try {
      const response = await fetch(url, { cache: "force-cache" });
      if (!response.ok) return;
      const encoded = await response.arrayBuffer();
      if (this.contextIsClosed()) return;
      const decoded = await this.context.decodeAudioData(encoded.slice(0));
      if (!this.contextIsClosed()) this.feedbackBuffers.set(key, decoded);
    } catch {
      // Feedback is optional. Asset/network/decode failures never fail a call.
    }
  }

  async playFeedback(
    turnId: string,
    generationId: string,
    cueId: string,
    key: string,
  ): Promise<number | null> {
    if (!this.isCurrent(turnId, generationId)) return null;
    const revision = this.revision;
    const generation = this.feedbackLifecycle.begin(turnId, cueId);
    const buffer = this.feedbackBuffers.get(key);
    if (!buffer || this.contextIsClosed()) return null;
    try {
      const cancelled = this.invalidated;
      const resumed = await Promise.race([
        this.context.resume().then(() => true), cancelled.then(() => false),
      ]);
      if (!resumed || revision !== this.revision || !this.isCurrent(turnId, generationId)) return null;
    } catch {
      return null;
    }
    if (!this.feedbackLifecycle.canStart(turnId, cueId, generation)) return null;

    this.stopFeedbackSource(0);
    const source = this.context.createBufferSource();
    source.buffer = buffer;
    source.connect(this.feedbackGain);
    this.feedbackGain.gain.cancelScheduledValues(this.context.currentTime);
    this.feedbackGain.gain.setValueAtTime(0.45, this.context.currentTime);
    const startTime = this.context.currentTime + 0.005;
    const contextToPerformanceOffset = performance.now() - this.context.currentTime * 1000;
    this.feedbackSource = source;
    source.onended = () => {
      if (this.feedbackSource === source) this.feedbackSource = null;
    };
    source.start(startTime);
    return contextToPerformanceOffset + startTime * 1000;
  }

  cancelFeedback(turnId: string, cueId: string): void {
    if (!this.feedbackLifecycle.cancel(turnId, cueId)) return;
    this.stopFeedbackSource(0.02);
  }

  cancelAllFeedback(): void {
    this.feedbackLifecycle.invalidate();
    this.stopFeedbackSource(0.02);
  }

  private stopFeedbackSource(fadeSeconds: number): void {
    const source = this.feedbackSource;
    if (source == null || this.contextIsClosed()) return;
    const now = this.context.currentTime;
    this.feedbackGain.gain.cancelScheduledValues(now);
    this.feedbackGain.gain.setValueAtTime(this.feedbackGain.gain.value, now);
    this.feedbackGain.gain.linearRampToValueAtTime(0, now + fadeSeconds);
    try {
      source.stop(now + fadeSeconds);
    } catch {
      // A source may have ended between lookup and stop().
    }
    this.feedbackSource = null;
  }

  async whenIdle(): Promise<void> {
    if (this.sources.size === 0) return;
    await new Promise<void>((resolve) => this.idleWaiters.push(resolve));
  }

  invalidate(): void {
    this.revision += 1;
    this.cancelResume();
    this.invalidated = new Promise<void>((resolve) => { this.cancelResume = resolve; });
    if (this.active) this.retired.add(this.active.generationId);
    this.active = null;
    this.cancelAllFeedback();
    this.sources.forEach((source) => {
      try {
        source.stop();
      } catch {
        // A source may have ended between the set lookup and stop().
      }
    });
    this.sources.clear();
    this.idleWaiters.splice(0).forEach((resolve) => resolve());
    this.nextStartTime = this.context.currentTime;
  }

  async close(): Promise<void> {
    this.closed = true;
    this.invalidate();
    if (this.context.state !== "closed") {
      await this.context.close();
    }
  }

  private contextIsClosed(): boolean {
    return this.context.state === "closed";
  }
}

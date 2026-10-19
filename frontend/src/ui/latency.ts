import type { TurnMetricsEvent } from "../voice/protocol";

// Stage keys match TurnTimer.waterfall() on the backend and the /analytics
// latency rollup. Hues walk the design's oklch ramp (262 → 172).
export const LATENCY_STAGES: Array<{ key: string; name: string; color: string }> = [
  { key: "endpoint_window_ms", name: "Endpoint window", color: "oklch(0.62 0.17 262)" },
  { key: "stt_ms", name: "STT finalize", color: "oklch(0.62 0.17 247)" },
  { key: "llm_ttft_ms", name: "LLM TTFT", color: "oklch(0.62 0.17 232)" },
  { key: "first_speakable_ms", name: "First speakable", color: "oklch(0.62 0.17 217)" },
  { key: "tts_connection_wait_ms", name: "TTS conn. wait", color: "oklch(0.62 0.17 202)" },
  { key: "tts_ttfb_ms", name: "TTS TTFB", color: "oklch(0.62 0.17 187)" },
  { key: "transport_playback_ms", name: "Transport + play", color: "oklch(0.62 0.17 172)" },
];

export type WaterfallStage = {
  key: string;
  name: string;
  color: string;
  startMs: number;
  durationMs: number | null;
};

export function waterfallStages(stages: Record<string, number | null>): WaterfallStage[] {
  let cursor = 0;
  return LATENCY_STAGES.map((stage) => {
    const duration = stages[stage.key] ?? null;
    const startMs = cursor;
    if (duration != null && duration > 0) cursor += duration;
    return { ...stage, startMs, durationMs: duration };
  });
}

export function turnE2e(metric: TurnMetricsEvent): number | null {
  const value = metric.stages.e2e_voice_to_voice_ms;
  if (value != null) return value;
  const summed = waterfallStages(metric.stages).reduce(
    (total, stage) => total + (stage.durationMs ?? 0),
    0,
  );
  return summed > 0 ? summed : null;
}

export function turnNumber(turnId: string): string {
  return turnId.split(":").at(-1) ?? turnId;
}

export function turnIndexOf(turnId: string): number {
  const value = Number(turnNumber(turnId));
  return Number.isFinite(value) ? value : 0;
}

export function e2eColor(valueMs: number | null): string {
  if (valueMs == null) return "#6B6B66";
  return valueMs > 1500 ? "#B3352E" : "#111110";
}

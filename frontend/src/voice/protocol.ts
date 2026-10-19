const CAPTURE_MAGIC = [0x54, 0x4c, 0x4b, 0x43] as const;
const CAPTURE_VERSION = 1;
const CAPTURE_HEADER_BYTES = 21;

export type AudioHeader = {
  turn_id: string;
  generation_id: string;
  sequence: number;
  codec: "pcm_s16le";
  sample_rate_hz: number;
  kind: "answer" | "cue";
};

export type AudioFrame = { header: AudioHeader; payload: ArrayBuffer };

export function decodeOutputAudio(frame: ArrayBuffer): AudioFrame {
  if (frame.byteLength < 11) throw new Error("Truncated audio frame");
  const view = new DataView(frame);
  if (view.getUint32(0, false) !== 0x544c4b53 || view.getUint8(4) !== 2) {
    throw new Error("Unsupported audio protocol");
  }
  const size = view.getUint32(5, false);
  const payloadSize = frame.byteLength - 9 - size;
  if (size === 0 || size > 4096 || payloadSize <= 0 || payloadSize > 256 * 1024 || payloadSize % 2) {
    throw new Error("Invalid audio frame length");
  }
  const parsed: unknown = JSON.parse(
    new TextDecoder("utf-8", { fatal: true }).decode(new Uint8Array(frame, 9, size)),
  );
  if (parsed == null || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error("Invalid audio header");
  }
  const header = parsed as Record<string, unknown>;
  const fields = ["turn_id", "generation_id", "sequence", "codec", "sample_rate_hz", "kind"];
  if (Object.keys(header).length !== fields.length || fields.some((key) => !(key in header))) {
    throw new Error("Invalid audio header fields");
  }
  for (const key of ["turn_id", "generation_id"]) {
    const value = header[key];
    if (
      typeof value !== "string" ||
      [...value].length < 1 || [...value].length > 128 ||
      /\p{Surrogate}/u.test(value)
    ) {
      throw new Error("Invalid audio identity");
    }
  }
  if (
    typeof header.sequence !== "number" || !Number.isInteger(header.sequence) ||
    header.sequence < 0 || header.sequence >= 2**32 ||
    header.codec !== "pcm_s16le" || (header.kind !== "answer" && header.kind !== "cue") ||
    typeof header.sample_rate_hz !== "number" ||
    ![8000, 16000, 22050, 24000, 44100, 48000].includes(header.sample_rate_hz)
  ) {
    throw new Error("Invalid audio profile or sequence");
  }
  return { header: Object.freeze(header) as AudioHeader, payload: frame.slice(9 + size) };
}

export function encodeCaptureFrame(
  captureSeq: number,
  captureTimeMs: number,
  pcm: ArrayBuffer,
): ArrayBuffer {
  const message = new ArrayBuffer(CAPTURE_HEADER_BYTES + pcm.byteLength);
  const view = new DataView(message);
  CAPTURE_MAGIC.forEach((byte, index) => view.setUint8(index, byte));
  view.setUint8(4, CAPTURE_VERSION);
  view.setBigUint64(5, BigInt(captureSeq), false);
  view.setFloat64(13, captureTimeMs, false);
  new Uint8Array(message, CAPTURE_HEADER_BYTES).set(new Uint8Array(pcm));
  return message;
}

export type ServerEvent =
  | {
      type: "ready";
      session_id: string;
      balance_paise: number;
      price_per_minute_paise: number;
      tts_sample_rate_hz: number;
      endpointing_strategy: "local_vad" | "sarvam";
      vad_model?: string;
      capabilities?: string[];
    }
  | {
      type: "billing";
      session_id: string;
      seconds: number;
      cost_paise: number;
      session_cost_paise: number;
      charged_paise: number;
      balance_paise: number;
      low_balance: boolean;
      warning: "low_balance" | null;
      final: boolean;
      terminated_reason: "balance_exhausted" | null;
    }
  | {
      type: "agent_state";
      state: "listening" | "user_speaking" | "thinking" | "speaking" | "interrupted";
      transmitting?: boolean;
      transport_status?: "transmitting_speech" | "silence_not_transmitting";
      detail?: string;
    }
  | {
      type: "final_transcript";
      generation_id: string;
      turn_id: string;
      text: string;
      language_code?: string | null;
      language_probability?: number | null;
    }
  | { type: "agent_text"; text: string; turn_id: string; generation_id: string }
  | { type: "search_results"; turn_id: string; generation_id: string; call_id: string; results: SearchSource[] }
  | {
      type: "audio_start";
      generation_id: string;
      turn_id: string;
      last_speech_capture_seq: number;
      last_speech_capture_time_ms: number;
      speech_anchor_source?: string;
    }
  | {
      type: "response_cue";
      generation_id: string;
      turn_id: string;
      cue_id: string;
      cue_key: "neutral_ack";
      language_code: string;
      delay_ms: number;
      last_speech_capture_time_ms: number;
    }
  | {
      type: "response_cue_cancel";
      generation_id: string;
      turn_id: string;
      cue_id: string;
      reason: "answer_started" | "call_ended" | "new_user_speech" | "turn_cancelled";
    }
  | { type: "turn_complete"; turn_id: string; generation_id: string }
  | { type: "interrupt"; turn_id: string; generation_id: string; reason: "barge_in" | "provider_failure" }
  | {
      type: "interrupt_resolved";
      turn_id: string;
      barge_in_stop_ack_ms: number | null;
      played_audio_ms: number;
      tts_socket_teardown_status: "closing" | "closed_and_discarded" | "already_closed";
    }
  | {
      type: "tool_request";
      call_id: string;
      turn_id: string;
      generation_id: string;
      request_hash: string;
      name: "todo_add" | "todo_list" | "todo_update" | "todo_complete" | "todo_delete";
      arguments: Record<string, unknown>;
    }
  | { type: "tool_receipt_query"; call_id: string; request_id: string; request_hash: string }
  | TurnMetricsEvent
  | { type: "call_ended"; reason: string; message?: string }
  | { type: "error"; code?: string; message: string; balance_paise?: number; turn_id?: string; generation_id?: string | null };

export type SearchSource = { title: string; url: string; image: string | null; favicon: string | null };

const httpsOrNull = (value: unknown) => value === null || (typeof value === "string" && value.startsWith("https://") && value.length <= 2048);

export type TurnMetricsEvent = {
  type: "turn_metrics";
  turn_id: string;
  endpointing_strategy: "local_vad" | "sarvam";
  stages: Record<string, number | null>;
  derived: Record<string, number | null>;
  dimensions: Record<string, unknown>;
  tool_spans: Array<{
    name: string;
    call_id: string;
    duration_ms: number;
    outcome: string;
  }>;
  missing_timestamps: string[];
};

export function parseServerEvent(message: string): ServerEvent {
  const parsed = JSON.parse(message) as unknown;
  if (typeof parsed !== "object" || parsed == null || !("type" in parsed)) {
    throw new Error("Invalid voice server event");
  }
  const event = parsed as Record<string, unknown>;
  if (event.type === "call_ended" && event.message !== undefined && typeof event.message !== "string") {
    throw new Error("Invalid call termination message");
  }
  if (event.type === "tool_request" || event.type === "tool_receipt_query") {
    const ids = event.type === "tool_request" ? ["call_id", "turn_id", "generation_id"] : ["call_id", "request_id"];
    if (ids.some(key => typeof event[key] !== "string" || !event[key] || [...event[key] as string].length > 128 || /\p{Surrogate}/u.test(event[key] as string)) ||
        typeof event.request_hash !== "string" || !/^[a-f0-9]{64}$/.test(event.request_hash)) {
      throw new Error("Invalid tool execution identity");
    }
    if (event.type === "tool_request" && (!['todo_add', 'todo_list', 'todo_update', 'todo_complete', 'todo_delete'].includes(String(event.name)) ||
        typeof event.arguments !== "object" || event.arguments === null || Array.isArray(event.arguments))) {
      throw new Error("Invalid todo request");
    }
  }
  if (event.type === "search_results" && (!Array.isArray(event.results) || event.results.length > 10 ||
      !event.results.every((item: Record<string, unknown>) => item != null && typeof item.title === "string" &&
        typeof item.url === "string" && /^https?:\/\//.test(item.url) && httpsOrNull(item.image) && httpsOrNull(item.favicon)))) {
    throw new Error("Invalid search results");
  }
  if (["final_transcript", "agent_text", "search_results", "audio_start", "response_cue", "response_cue_cancel", "interrupt", "turn_complete"].includes(String(event.type))) {
    for (const key of ["turn_id", "generation_id"]) {
      const value = event[key];
      if (typeof value !== "string" || !value || [...value].length > 128 || /\p{Surrogate}/u.test(value)) {
        throw new Error("Invalid audio event identity");
      }
    }
  }
  if (event.type === "response_cue") {
    if (
      typeof event.turn_id !== "string" ||
      typeof event.cue_id !== "string" ||
      event.cue_key !== "neutral_ack" ||
      typeof event.language_code !== "string" ||
      typeof event.delay_ms !== "number" ||
      !Number.isFinite(event.delay_ms) ||
      event.delay_ms < 0 ||
      typeof event.last_speech_capture_time_ms !== "number" ||
      !Number.isFinite(event.last_speech_capture_time_ms)
    ) {
      throw new Error("Invalid response cue event");
    }
  }
  if (
    event.type === "response_cue_cancel" &&
    (typeof event.turn_id !== "string" || typeof event.cue_id !== "string")
  ) {
    throw new Error("Invalid response cue cancellation");
  }
  return event as ServerEvent;
}

type WorkletPcmMessage = {
  pcm: ArrayBuffer;
  captureTimeMs: number;
};

export class MicrophoneCapture {
  private context: AudioContext | null = null;
  private stream: MediaStream | null = null;
  private source: MediaStreamAudioSourceNode | null = null;
  private worklet: AudioWorkletNode | null = null;

  async start(onFrame: (pcm: ArrayBuffer, captureTimeMs: number) => void): Promise<void> {
    this.stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    });
    this.context = new AudioContext();
    await this.context.audioWorklet.addModule("/pcm-capture-worklet.js");
    const performanceTimeOriginMs = performance.now() - this.context.currentTime * 1000;
    this.worklet = new AudioWorkletNode(this.context, "pcm-capture", {
      processorOptions: {
        targetSampleRate: 16000,
        frameSamples: 512,
        performanceTimeOriginMs,
      },
    });
    this.worklet.port.onmessage = (event: MessageEvent<WorkletPcmMessage>) => {
      onFrame(event.data.pcm, event.data.captureTimeMs);
    };
    this.source = this.context.createMediaStreamSource(this.stream);
    this.source.connect(this.worklet);
    this.worklet.connect(this.context.destination);
    await this.context.resume();
  }

  async stop(): Promise<void> {
    this.source?.disconnect();
    this.worklet?.disconnect();
    this.stream?.getTracks().forEach((track) => track.stop());
    if (this.context && this.context.state !== "closed") {
      await this.context.close();
    }
    this.context = null;
    this.stream = null;
    this.source = null;
    this.worklet = null;
  }
}

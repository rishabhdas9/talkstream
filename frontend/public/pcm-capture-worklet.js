class PcmCaptureProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const config = options.processorOptions ?? {};
    this.targetSampleRate = config.targetSampleRate ?? 16000;
    this.frameSamples = config.frameSamples ?? 512;
    this.performanceTimeOriginMs = config.performanceTimeOriginMs ?? 0;
    this.ratio = sampleRate / this.targetSampleRate;
    this.sourceSamples = [];
    this.sourcePosition = 0;
    this.outputSamples = [];
  }

  process(inputs) {
    const input = inputs[0]?.[0];
    if (!input) return true;
    for (const sample of input) this.sourceSamples.push(sample);

    while (this.sourcePosition + this.ratio <= this.sourceSamples.length) {
      const start = Math.floor(this.sourcePosition);
      const end = Math.max(start + 1, Math.floor(this.sourcePosition + this.ratio));
      let total = 0;
      for (let index = start; index < end; index += 1) total += this.sourceSamples[index];
      this.outputSamples.push(total / (end - start));
      this.sourcePosition += this.ratio;
    }

    const consumed = Math.floor(this.sourcePosition);
    if (consumed > 0) {
      this.sourceSamples.splice(0, consumed);
      this.sourcePosition -= consumed;
    }

    while (this.outputSamples.length >= this.frameSamples) {
      const frame = this.outputSamples.splice(0, this.frameSamples);
      const pcm = new Int16Array(this.frameSamples);
      frame.forEach((sample, index) => {
        const clamped = Math.max(-1, Math.min(1, sample));
        pcm[index] = clamped < 0 ? clamped * 32768 : clamped * 32767;
      });
      const captureTimeMs =
        this.performanceTimeOriginMs + currentTime * 1000 - (this.frameSamples / this.targetSampleRate) * 1000;
      this.port.postMessage({ pcm: pcm.buffer, captureTimeMs }, [pcm.buffer]);
    }
    return true;
  }
}

registerProcessor("pcm-capture", PcmCaptureProcessor);

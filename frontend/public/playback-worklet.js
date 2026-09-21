// Plays queued Float32 chunks. Prefills ~60 ms before playing (and again after an underrun);
// silence while priming, drops the oldest audio when over ~120 ms.
class PlaybackProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.queue = [];
    this.offset = 0;
    this.buffered = 0;
    this.maxBuffered = Math.round(sampleRate * 0.12);
    this.prefill = Math.round(sampleRate * 0.06);
    this.priming = true;
    this.port.onmessage = (event) => {
      this.queue.push(event.data);
      this.buffered += event.data.length;
      if (this.buffered >= this.prefill) this.priming = false;
      while (this.buffered > this.maxBuffered && this.queue.length > 1) {
        const dropped = this.queue.shift();
        this.buffered -= dropped.length - this.offset;
        this.offset = 0;
      }
    };
  }

  process(_inputs, outputs) {
    const out = outputs[0][0];
    for (let i = 0; i < out.length; i++) {
      if (this.priming || this.queue.length === 0) {
        if (this.queue.length === 0) this.priming = true; // underrun: rebuild depth first
        out[i] = 0;
        continue;
      }
      const head = this.queue[0];
      out[i] = head[this.offset++];
      this.buffered--;
      if (this.offset >= head.length) {
        this.queue.shift();
        this.offset = 0;
      }
    }
    return true;
  }
}
registerProcessor("playback", PlaybackProcessor);

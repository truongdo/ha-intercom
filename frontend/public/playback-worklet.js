// Plays queued Float32 chunks. Prefills ~150 ms before playing (and again after an underrun);
// silence while priming, drops the oldest audio back to ~150 ms when over ~300 ms. That's wider
// than it looks necessary for on paper: the goal isn't just absorbing jitter, it's giving the buffer enough
// depth that an underrun/overflow event (see below) is rare, not just quiet when it happens.
//
// The first silent sample after real audio, and the first real sample after silence, are
// ramped over ~8 ms rather than cut, so an underrun (or the initial buffer priming) doesn't
// sound like a click. An overflow drop is the same kind of discontinuity — it splices from
// wherever playback currently is straight into an unrelated, later chunk — so it crossfades
// from the last played sample into the new chunk's own samples over the same ~8 ms, while
// still consuming the queue at the normal rate (pausing consumption to fade would let the
// queue overflow again before the fade even finishes).
//
// The ramp itself is a raised cosine, not a straight line: a linear ramp's *value* is
// continuous but its *slope* still jumps at both ends, and that corner is itself audible as a
// soft click on short fades. Zero slope at both ends is what actually makes it inaudible.
class PlaybackProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.queue = [];
    this.offset = 0;
    this.buffered = 0;
    this.maxBuffered = Math.round(sampleRate * 0.3);
    this.prefill = Math.round(sampleRate * 0.15);
    this.priming = true;
    this.fadeLen = Math.max(1, Math.round(sampleRate * 0.008));
    this.wasSilent = true;
    // Last value actually output (after any fade). Every fade starts from here, not from the
    // last real sample or from 0: a new fade can begin while another is still mid-ramp (e.g.
    // an underrun's fade-out cut short by a burst arriving), and restarting from anything but
    // the current output value would itself be a jump.
    this.lastOut = 0;
    this.fadeFrom = 0;
    this.fadeSpan = 0;
    this.fadeLeft = 0;
    this.port.onmessage = (event) => {
      this.queue.push(event.data);
      this.buffered += event.data.length;
      if (this.buffered >= this.prefill) this.priming = false;
      if (this.buffered > this.maxBuffered) {
        // Trim back to the prefill depth in one go (one splice, one crossfade) rather than
        // just under the cap: a bursty link (TCP on cellular) would otherwise re-overflow and
        // splice again on nearly every chunk of the burst.
        while (this.buffered > this.prefill && this.queue.length > 1) {
          const dropped = this.queue.shift();
          this.buffered -= dropped.length - this.offset;
          this.offset = 0;
        }
        this._startFade(this.lastOut);
      }
    };
  }

  _startFade(from) {
    this.fadeFrom = from;
    this.fadeSpan = this.fadeLen;
    this.fadeLeft = this.fadeLen;
  }

  process(_inputs, outputs) {
    const out = outputs[0][0];
    for (let i = 0; i < out.length; i++) {
      const silentNow = this.priming || this.queue.length === 0;
      if (silentNow) {
        if (this.queue.length === 0) this.priming = true; // underrun: rebuild depth first
        if (!this.wasSilent) this._startFade(this.lastOut);
        this.wasSilent = true;
      } else {
        if (this.wasSilent) this._startFade(this.lastOut); // incl. the very first audio
        this.wasSilent = false;
      }

      let sample;
      if (silentNow) {
        sample = 0;
      } else {
        const head = this.queue[0];
        sample = head[this.offset++];
        this.buffered--;
        if (this.offset >= head.length) {
          this.queue.shift();
          this.offset = 0;
        }
      }

      if (this.fadeLeft > 0) {
        const t = 1 - this.fadeLeft / this.fadeSpan;
        const s = 0.5 - 0.5 * Math.cos(t * Math.PI); // raised cosine: 0->1, zero slope at both ends
        // Entering silence: ramp from fadeFrom (the last output value) down to 0. Otherwise:
        // ramp from fadeFrom into the real sample being played.
        sample = silentNow ? this.fadeFrom * (1 - s) : sample * s + this.fadeFrom * (1 - s);
        this.fadeLeft--;
      }
      out[i] = sample;
      this.lastOut = sample;
    }
    return true;
  }
}
registerProcessor("playback", PlaybackProcessor);

export function floatToInt16(input: Float32Array): Int16Array {
  const out = new Int16Array(input.length);
  for (let i = 0; i < input.length; i++) {
    const s = Math.max(-1, Math.min(1, input[i]));
    out[i] = s < 0 ? Math.round(s * 32768) : Math.round(s * 32767);
  }
  return out;
}

export function int16ToFloat(input: Int16Array): Float32Array {
  const out = new Float32Array(input.length);
  for (let i = 0; i < input.length; i++) {
    out[i] = input[i] / (input[i] < 0 ? 32768 : 32767);
  }
  return out;
}

/**
 * Stateful resampler for chunked streams. When downsampling it first applies a running mean
 * about `srcRate/dstRate` samples wide (a cheap anti-alias low-pass), then linearly interpolates.
 */
export class StreamResampler {
  private readonly step: number;
  private readonly width: number;
  private history: number[];
  private pos = 0;
  private last = 0;

  constructor(srcRate: number, dstRate: number) {
    this.step = srcRate / dstRate;
    this.width = this.step > 1 ? Math.max(2, Math.round(this.step)) : 1;
    this.history = new Array(this.width - 1).fill(0);
  }

  private lowpass(input: Float32Array): Float32Array {
    if (this.width === 1) return input;
    const w = this.width;
    const extended = new Float32Array(this.history.length + input.length);
    extended.set(this.history, 0);
    extended.set(input, this.history.length);
    const out = new Float32Array(input.length);
    for (let i = 0; i < input.length; i++) {
      let sum = 0;
      for (let k = 0; k < w; k++) sum += extended[i + k];
      out[i] = sum / w;
    }
    this.history = Array.from(extended.subarray(extended.length - (w - 1)));
    return out;
  }

  process(rawInput: Float32Array): Float32Array {
    const input = this.lowpass(rawInput);
    const out: number[] = [];
    // Virtual buffer v = [last, ...input]; pos indexes into v.
    while (Math.floor(this.pos) + 1 <= input.length) {
      const i = Math.floor(this.pos);
      const frac = this.pos - i;
      const a = i === 0 ? this.last : input[i - 1];
      const b = input[i];
      out.push(a * (1 - frac) + b * frac);
      this.pos += this.step;
    }
    this.pos -= input.length;
    if (input.length > 0) this.last = input[input.length - 1];
    return Float32Array.from(out);
  }
}

/** Collects samples into fixed-size frames. */
export class Framer {
  private pending: number[] = [];

  constructor(private readonly frameSamples: number) {}

  push(samples: Int16Array): Int16Array[] {
    for (const s of samples) this.pending.push(s);
    const frames: Int16Array[] = [];
    while (this.pending.length >= this.frameSamples) {
      frames.push(Int16Array.from(this.pending.splice(0, this.frameSamples)));
    }
    return frames;
  }
}

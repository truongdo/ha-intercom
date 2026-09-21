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

/** Stateful linear-interpolation resampler for chunked streams. */
export class StreamResampler {
  private readonly step: number;
  private pos = 0;
  private last = 0;

  constructor(srcRate: number, dstRate: number) {
    this.step = srcRate / dstRate;
  }

  process(input: Float32Array): Float32Array {
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

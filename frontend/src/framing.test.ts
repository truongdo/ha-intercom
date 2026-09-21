import { describe, expect, it } from "vitest";
import { Framer, StreamResampler, floatToInt16, int16ToFloat } from "./framing";

describe("int16/float conversion", () => {
  it("clamps and scales", () => {
    expect(Array.from(floatToInt16(new Float32Array([0, 1, -1, 2, -2])))).toEqual([
      0, 32767, -32768, 32767, -32768,
    ]);
  });

  it("round-trips within one step", () => {
    const back = int16ToFloat(floatToInt16(new Float32Array([0.5, -0.25])));
    expect(back[0]).toBeCloseTo(0.5, 3);
    expect(back[1]).toBeCloseTo(-0.25, 3);
  });
});

describe("StreamResampler", () => {
  it("decimates by two as a continuous stream, averaging pairs first", () => {
    const r = new StreamResampler(32000, 16000);
    const first = r.process(Float32Array.from([1, 2, 3, 4, 5, 6, 7, 8]));
    const second = r.process(Float32Array.from([9, 10, 11, 12, 13, 14, 15, 16]));
    // Each output is the mean of the two source samples it spans (running mean of width 2).
    expect([...first, ...second]).toEqual([0, 1.5, 3.5, 5.5, 7.5, 9.5, 11.5, 13.5]);
  });

  it("attenuates a tone above the destination Nyquist instead of aliasing it", () => {
    const n = 4800;
    // 15 kHz at 48 kHz folds to 1 kHz at 16 kHz when naively decimated.
    const tone = Float32Array.from({ length: n }, (_, i) => Math.sin((2 * Math.PI * 15000 * i) / 48000));
    const out = new StreamResampler(48000, 16000).process(tone);
    const rms = Math.sqrt(out.reduce((a, v) => a + v * v, 0) / out.length);
    expect(rms).toBeLessThan(0.2);
  });

  it("passes voice-band tones and DC through nearly unchanged", () => {
    const n = 4800;
    const tone = Float32Array.from({ length: n }, (_, i) => Math.sin((2 * Math.PI * 300 * i) / 48000));
    const out = new StreamResampler(48000, 16000).process(tone);
    const rms = Math.sqrt(out.reduce((a, v) => a + v * v, 0) / out.length);
    expect(rms).toBeGreaterThan(0.68);
    const dc = new StreamResampler(48000, 16000).process(new Float32Array(300).fill(0.5));
    expect(dc[dc.length - 1]).toBeCloseTo(0.5, 6);
  });

  it("gives the same output regardless of chunking", () => {
    const input = Float32Array.from({ length: 480 }, (_, i) => Math.sin(i / 7));
    const whole = new StreamResampler(48000, 16000).process(input);
    const chunked = new StreamResampler(48000, 16000);
    const parts: number[] = [];
    for (let i = 0; i < input.length; i += 128) {
      parts.push(...chunked.process(input.slice(i, i + 128)));
    }
    expect(parts.length).toBe(whole.length);
    parts.forEach((v, i) => expect(v).toBeCloseTo(whole[i], 6));
  });

  it("upsamples by interpolation", () => {
    const r = new StreamResampler(16000, 32000);
    const out = r.process(Float32Array.from([2, 4]));
    // The stream starts from an implicit previous sample of 0: 0->2 passes 1, 2->4 passes 3.
    expect([...out]).toEqual([0, 1, 2, 3]);
  });
});

describe("Framer", () => {
  it("emits fixed-size frames and keeps the remainder", () => {
    const f = new Framer(4);
    expect(f.push(Int16Array.from([1, 2, 3]))).toEqual([]);
    const frames = f.push(Int16Array.from([4, 5, 6, 7, 8, 9]));
    expect(frames.map((x) => Array.from(x))).toEqual([
      [1, 2, 3, 4],
      [5, 6, 7, 8],
    ]);
    expect(f.push(Int16Array.from([10, 11, 12])).map((x) => Array.from(x))).toEqual([
      [9, 10, 11, 12],
    ]);
  });
});

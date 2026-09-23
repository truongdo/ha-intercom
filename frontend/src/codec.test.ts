import { describe, expect, it } from "vitest";
import { createOpusCodec, resolveWireCodec } from "./codec";

function tone(frame: number): Int16Array {
  const out = new Int16Array(320);
  for (let i = 0; i < 320; i++) {
    out[i] = Math.round(8000 * Math.sin((2 * Math.PI * 440 * (frame * 320 + i)) / 16000));
  }
  return out;
}

describe("createOpusCodec", () => {
  it("round-trips 20 ms frames as small packets", async () => {
    const codec = await createOpusCodec();
    let energy = 0;
    for (let f = 0; f < 25; f++) {
      const packet = codec.encode(tone(f));
      expect(packet.length).toBeGreaterThan(0);
      expect(packet.length).toBeLessThan(100);
      const pcm = codec.decode(packet);
      expect(pcm.length).toBe(320);
      if (f >= 5) for (const s of pcm) energy += s * s;
    }
    expect(Math.sqrt(energy / (20 * 320))).toBeGreaterThan(2000); // the tone survived (RMS ~5657)
    codec.close();
  });

  it("keeps sending packets for silence (no DTX)", async () => {
    const codec = await createOpusCodec();
    for (let f = 0; f < 10; f++) expect(codec.encode(new Int16Array(320)).length).toBeGreaterThan(0);
    codec.close();
  });

  it("rejects packets that are not one 20 ms frame", async () => {
    const codec = await createOpusCodec();
    expect(() => codec.decode(new Uint8Array(0))).toThrow();
    expect(() => codec.decode(new Uint8Array(640))).toThrow(); // PCM sent as Opus: 160 samples
    codec.close();
  });

  it("close is idempotent", async () => {
    const codec = await createOpusCodec();
    codec.close();
    expect(() => codec.close()).not.toThrow();
  });

  it("throws instead of using the codec after close", async () => {
    const codec = await createOpusCodec();
    codec.close();
    expect(() => codec.encode(new Int16Array(320))).toThrow("codec closed");
    expect(() => codec.decode(new Uint8Array([0x78, 0]))).toThrow("codec closed");
  });
});

describe("resolveWireCodec", () => {
  it("uses opus only when requested and confirmed", () => {
    expect(resolveWireCodec(true, "opus")).toBe("opus");
    expect(resolveWireCodec(true, "pcm")).toBe("pcm");
    expect(resolveWireCodec(true, undefined)).toBe("pcm"); // older server: no codec field
    expect(resolveWireCodec(false, "opus")).toBe("pcm"); // never decode what we can't
  });
});

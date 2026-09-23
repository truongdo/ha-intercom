// Opus at the WebSocket edge (see the backend's audio/opus.py for the host side). The settings
// must match the host: 16 kHz mono, 20 ms frames, VOIP, 20 kbps VBR, complexity 5, no DTX.
const RATE = 16000;
const FRAME_SAMPLES = 320;
const BITRATE = 20000;
const COMPLEXITY = 5;

export type WireCodec = "pcm" | "opus";

export interface AudioCodec {
  /** One 320-sample frame in, one packet out. */
  encode(pcm: Int16Array): Uint8Array;
  /** One packet in, one 320-sample frame out; throws on anything else. */
  decode(packet: Uint8Array): Int16Array;
  close(): void;
}

/** Loads the WASM libopus (a separate chunk, fetched on first call) and creates a codec pair. */
export async function createOpusCodec(): Promise<AudioCodec> {
  const { Encoder, Decoder } = await import("@evan/wasm/target/opus/deno.js");
  const encoder = new Encoder({ channels: 1, sample_rate: RATE, application: "voip" });
  let decoder: InstanceType<typeof Decoder>;
  try {
    encoder.bitrate = BITRATE;
    encoder.complexity = COMPLEXITY;
    encoder.vbr = true;
    // No DTX: the server's idle timeout expects a steady 50 frames/s even when muted.
    encoder.dtx = false;
    decoder = new Decoder({ channels: 1, sample_rate: RATE });
  } catch (err) {
    encoder.drop();
    throw err;
  }
  let closed = false;
  return {
    encode: (pcm) => encoder.encode(pcm),
    decode(packet) {
      // libopus is lenient: empty input would be concealed, other junk often "decodes".
      if (packet.length === 0) throw new Error("empty opus packet");
      const bytes = decoder.decode(packet);
      if (bytes.length !== FRAME_SAMPLES * 2) {
        throw new Error(`opus packet decoded to ${bytes.length / 2} samples, expected ${FRAME_SAMPLES}`);
      }
      return new Int16Array(bytes.buffer, bytes.byteOffset, FRAME_SAMPLES);
    },
    close() {
      if (closed) return;
      closed = true;
      encoder.drop();
      decoder.drop();
    },
  };
}

/** The codec actually in use: Opus only if we asked for it and the server's ready confirmed it. */
export function resolveWireCodec(requestedOpus: boolean, readyCodec: unknown): WireCodec {
  return requestedOpus && readyCodec === "opus" ? "opus" : "pcm";
}

// @evan/wasm ships no types. This covers the part of its libopus build that codec.ts uses.
declare module "@evan/wasm/target/opus/deno.js" {
  export class Encoder {
    constructor(options?: {
      channels?: 1 | 2;
      sample_rate?: 8000 | 12000 | 16000 | 24000 | 48000;
      application?: "voip" | "audio" | "restricted_lowdelay";
    });
    bitrate: number;
    complexity: number;
    vbr: boolean;
    dtx: boolean;
    /** Encodes one frame of int16 PCM; returns a copy of the packet. */
    encode(pcm: ArrayBufferView): Uint8Array;
    drop(): void;
  }
  export class Decoder {
    constructor(options?: { channels?: 1 | 2; sample_rate?: 8000 | 12000 | 16000 | 24000 | 48000 });
    /** Decodes one packet; returns a copy of the int16 PCM as bytes. */
    decode(packet: ArrayBufferView): Uint8Array;
    drop(): void;
  }
}

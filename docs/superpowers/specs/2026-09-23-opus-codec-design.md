# Opus Audio Transport: Design

Date: 2026-09-23

## Goal

Replace raw PCM on the WebSocket with Opus to cut per-call bandwidth by roughly 10×
(about 256 kbps down to about 20 kbps of payload per direction), for three reasons the owner
gave equal weight:

- **A. Mobile data cost:** callers on cellular pay per MB.
- **B. Weak links:** smaller packets shrink TCP stalls on bursty cellular links, and Opus
  packet-loss concealment (PLC) can replace some of the silence played on an underrun.
- **C. Host uplink:** less load on the Cloudflare tunnel and the host's upstream connection.

Both desktop and mobile browsers must work, **including iPhone/iPad Safari**. Every iOS
browser uses WebKit, so native WebCodecs audio can't be relied on.

## Non-goals

- Switching to WebRTC or UDP. The transport stays one WebSocket (TCP).
- Opus inside the server's audio pipeline. ALSA, the echo canceller, the ringtone and the
  jitter buffer stay PCM. The codec lives only at the WebSocket edge.
- Browser-side PLC. Decoding stays on the main thread, and the playback worklet's existing
  underrun crossfade covers gaps.
- DTX (silence suppression), for the reasons given under "Opus settings".

## Target host (measured 2026-09-23)

| | |
|---|---|
| Board | Orange Pi PC Plus: 4× Cortex-A7 at up to 1.3 GHz (`ondemand`), armv7l with NEON |
| RAM | 991 MB total, 866 MB available |
| Idle load | 0.19; the intercom service at 0.5 % CPU, 29 MB RSS |
| libopus | `libopus0` 1.5.2-2 already installed at `/usr/lib/arm-linux-gnueabihf/libopus.so.0` |
| Python | 3.13.5 |

**CPU, measured on the host (2026-09-23).** 60 s of speech-like audio at 20 kbps, encoded and
decoded through a ctypes prototype of the wrapper. The figures include the Python loop
overhead:

| Complexity | CPU per 20 ms frame (encode + decode) | Share of one core | Bitrate |
|---|---|---|---|
| 5 | 1.73 ms | 8.6 % | 19.9 kbps |
| 8 | 2.53 ms | 12.6 % | 19.9 kbps |
| 10 | 2.52 ms | 12.6 % | 19.9 kbps |

Complexity 5 meets the target of under 10 % of one core, on a 4-core board.

## Opus settings

- 16 kHz mono (wideband), 20 ms frames (320 samples), matching today's frame timing.
- Application `OPUS_APPLICATION_VOIP`, 20 kbps VBR.
- Complexity 5, per the host measurement above.
- **No DTX.** The server's idle timeout and the jitter buffer both assume a steady 50 frames
  per second, so with DTX a silent caller would look like a dead one.
- The browser encoder uses the same settings.

## Wire format

**Handshake.** The client opens `/ws?codec=opus`. The server picks the codec and reports it
in `ready`:

```json
{"type": "ready", "rate": 16000, "channels": 1, "frame_ms": 20, "codec": "opus"}
```

- The server picks `"opus"` only when the client asked for it **and** libopus loaded at
  startup. Otherwise it picks `"pcm"`, which is today's format.
- The client follows `ready.codec`, whatever it asked for. That covers a new page talking to
  an old server during a deploy, and an old cached page (which sends no `codec`) talking to a
  new server.

**Frames.** Each binary message carries one frame of 20 ms of 16 kHz mono audio:

- `pcm`: 640 bytes of little-endian int16, as today.
- `opus`: exactly one raw Opus packet (no Ogg, no header), about 50 bytes at 20 kbps.

Both directions keep sending 50 messages a second.

## Components

### Backend

**`audio/opus.py` (new).** A ctypes wrapper around `libopus.so.0`, with no new Python
dependency.

- `load() -> bool`: finds and loads the library once. It returns `False` and logs a warning
  when libopus is missing. It tries `ctypes.util.find_library("opus")`, then `libopus.so.0`,
  then `/opt/homebrew/lib/libopus.0.dylib`, because `find_library` doesn't search Homebrew.
- `web.py` calls `load()` once when it builds the app and passes the result to
  `SessionManager(..., opus_available=...)`, which defaults to `False`.
- `OpusEncoder()`: `encode(pcm: bytes) -> bytes`. Takes exactly `FRAME_BYTES` of input.
- `OpusDecoder()`: `decode(packet: bytes) -> bytes` returns `FRAME_BYTES` of PCM, and
  `conceal() -> bytes` returns one PLC frame (`opus_decode` with a NULL packet).
- `OpusError(Exception)` is raised on any negative libopus return code. `decode()` also raises
  it in two cases where libopus does not:
  - **an empty packet**, which libopus would silently treat as a lost packet and conceal;
  - **any packet that decodes to anything other than exactly 320 samples**. libopus is
    lenient: a 640-byte PCM frame of zeros "decodes" to 160 samples, and arbitrary bytes often
    decode without error. The sample-count check is the practical validity check.
- Each object owns its libopus state and frees it in `close()`, with `__del__` as a safety
  net.
- The decoder has an internal `threading.Lock`, because `decode()` runs on the event loop and
  `conceal()` runs on the ALSA callback thread.

**`protocol.py`.** Adds `CODEC_PCM = "pcm"` and `CODEC_OPUS = "opus"`. `ready_message(codec)`
includes `"codec"`.

**`session.py`.**

- Reads the `codec` query parameter and resolves it as described in the wire format section,
  using whether libopus loaded at startup.
- For Opus, each call creates one `OpusEncoder` and one `OpusDecoder`, and closes them in the
  existing `finally`.
- **Receive (Opus):** `decoder.decode(data)` runs, then `jitter.push(pcm)`. Decoding on
  arrival keeps the decoder fed with packets in order, and frames the jitter buffer later
  drops on overflow have already gone through the decoder. The arrival-gap statistics are
  recorded for every frame accepted into the jitter buffer, the same as for PCM.
- **Receive (PCM):** unchanged, including the `len(data) == FRAME_BYTES` check.
- **Send (Opus):** `send_mic` encodes each frame before `ws.send_bytes`.
- **Jitter buffer:** constructed with `conceal=decoder.conceal` for Opus sessions.
- **Per-call log line:** adds `codec=` and `bad_packets=`.

**`audio/jitter.py`.**

- `JitterBuffer(..., conceal: Callable[[], bytes] | None = None)`.
- On an underrun **after real audio has played**, `pop()` returns up to `MAX_CONCEAL_FRAMES =
  2` frames (40 ms) from `conceal()`. The following frame fades from the last concealed
  sample, using the existing `_fade_out(self._last_sample)`.
- If audio arrives during concealment, the next real frame crossfades from the last concealed
  sample, just as after a splice.
- Priming and re-priming after an underrun don't change: concealment fills the gap while the
  buffer rebuilds depth, and then silence takes over.
- If `conceal()` raises, the buffer logs it and falls back to the current fade-to-silence.
- Concealed frames are counted in `stats()` as `concealed_frames`.
- With `conceal=None`, behaviour is byte-for-byte what it is today.

**`deploy/remote-setup.sh`.** `libopus0` is added to the `INSTALL_APT` package list, so a
fresh host gets it. The current host already has it, so this deploy doesn't need
`INSTALL_APT=1`.

### Frontend

**`src/codec.ts` (new).** Wraps a WASM Opus build behind one small interface:

```ts
interface AudioCodec {
  encode(pcm: Int16Array): Uint8Array;   // one 320-sample frame -> one packet
  decode(packet: Uint8Array): Int16Array; // one packet -> one 320-sample frame
  close(): void;
}
async function createOpusCodec(): Promise<AudioCodec>; // rejects if WASM can't load
```

- **Library:** `@evan/wasm` 0.0.95 (MIT), imported as `@evan/wasm/target/opus/deno.js`. Chosen
  by the owner on 2026-09-23.
  - It's a self-contained ES module with the libopus WASM inlined, and a synchronous raw-packet
    API: `new Encoder({channels, sample_rate, application})` with setters `bitrate`,
    `complexity`, `vbr`, `dtx`; `encode(view) -> Uint8Array`; `new Decoder({channels,
    sample_rate})`; `decode(view) -> Uint8Array` of int16 LE bytes; `drop()` frees the state.
  - It has shipped no updates since 2022, has no TypeScript types (the project adds a small
    `.d.ts`), and compiles its roughly 200 KB WASM synchronously when first imported.
    `codec.ts` therefore loads it with a dynamic `import()`, so it isn't on the page's critical
    path and a failure is catchable.
  - `@evan/opus` was the first candidate but is Node-only: its WASM loader calls
    `require('fs')`.
  - Verified in Node 22: at 20 kbps, 16 kHz mono and complexity 5, packets are 39–72 bytes and
    decode to 640 bytes. Verifying it on iPhone Safari is part of manual acceptance.
- The WASM is part of the frontend bundle, so no third-party CDN is involved.
- `decode()` applies the same exactly-320-samples check as the host.

**`src/intercom.ts`.**

- Before opening the WebSocket, `start()` calls `createOpusCodec()`.
  - On success it connects to `/ws?codec=opus`.
  - On failure it logs `console.warn("opus unavailable, using pcm", err)` and connects without
    the parameter.
- `ready.codec` decides the path:
  - **Send:** `Framer` output goes through `codec.encode` when the codec is Opus, and is sent
    as-is for PCM.
  - **Receive:** Opus packets go through `codec.decode` before `int16ToFloat`. Resampling,
    framing and the worklets don't change.
- The codec is freed in `finish()`, and `stale()` generation guards apply to it the same way
  they apply to the audio context.

## Error handling

| Case | Behaviour |
|---|---|
| libopus missing on the host | Startup warning; the server only offers `pcm`. The service stays up. |
| Received Opus packet fails to decode | Packet dropped and `bad_packets` incremented; the jitter buffer treats it as a late packet. It still counts toward the idle timeout, the same as malformed PCM today. |
| Host encode fails | Logged once per call; the frame is skipped and the call continues. |
| `conceal()` raises | Logged; the buffer falls back to fading to silence. |
| WASM fails to load in the browser | `console.warn` and the call uses PCM. |
| Browser decode fails | Packet skipped; the playback worklet's underrun crossfade covers the gap. |

## Testing

**Backend (pytest).** The Opus tests are skipped with a clear reason when libopus is absent
(`brew install opus` on the Mac).

- `test_opus.py`:
  - Round trip of a 440 Hz sine: the decoded output correlates with the input (allowing for
    codec delay), and the packet is under 100 bytes.
  - `conceal()` returns `FRAME_BYTES`.
  - An invalid packet (`b"\x03"`, which libopus rejects), an empty packet and a 640-byte PCM
    frame each raise `OpusError`.
  - Wrong-sized encoder input raises `ValueError`.
- `test_jitter.py`:
  - With `conceal`: an underrun yields at most 2 concealed frames, then a fade, then silence.
    The `concealed_frames` stat counts them.
    Audio arriving mid-concealment crossfades in.
    A `conceal` that raises falls back to fading.
  - Without `conceal`: every existing test passes unchanged.
- `test_session.py`:
  - `?codec=opus` gives `ready.codec == "opus"`, received packets are decoded into the jitter
    buffer, and mic frames are sent encoded.
  - No `codec` gives `pcm`, and every existing test passes unchanged.
  - A bad packet increments `bad_packets` without ending the call.
  - libopus unavailable means an Opus request gets `pcm`.

**Frontend (vitest).** `codec.test.ts`: WASM round trip in Node, a packet-size range check,
and `close()` being idempotent.

**On the host.** The codec benchmark is done (see Target host). After the deploy, run one
end-to-end call and watch the service's CPU in `top`. The expectation is roughly 10 % of one
core or less above today's figure.

**Manual acceptance.** Checked against the per-call log for `codec=opus`, `bad_packets=0`, and
underruns and concealed frames:
- Desktop Chrome over the SSH forward.
- iPhone Safari over the tunnel on **cellular**.
- Android Chrome over the tunnel.
- Bandwidth, before and after, from the DevTools WebSocket frames view. Expected: about
  256 kbps down to about 20 kbps per direction.

## Rollout

The server and client can be deployed in either order because of the codec negotiation. With
a single `install.sh` run the new page and server go out together. Pages cached from before
the deploy keep using PCM until reloaded.

import { Framer, StreamResampler, floatToInt16, int16ToFloat } from "./framing";

const WIRE_RATE = 16000;
const FRAME_SAMPLES = 320;

export type IntercomState = "idle" | "connecting" | "ringing" | "live" | "busy" | "error";
type OnState = (state: IntercomState, detail?: string) => void;

const ERROR_TEXT: Record<string, string> = {
  device_unavailable: "The audio device is unavailable.",
  device_lost: "The audio device was disconnected.",
  device_error: "The audio device reported an error.",
  idle_timeout: "Session ended: no audio received.",
  declined: "Call was declined.",
  timeout: "No answer.",
};

export class Intercom {
  private ws: WebSocket | null = null;
  private ctx: AudioContext | null = null;
  private stream: MediaStream | null = null;
  private playback: AudioWorkletNode | null = null;
  private upsampler: StreamResampler | null = null;
  private finished = false;
  /** Bumped on every start(); async continuations from an earlier session compare against it. */
  private generation = 0;

  constructor(private readonly onState: OnState) {}

  setMuted(muted: boolean): void {
    this.stream?.getAudioTracks().forEach((track) => (track.enabled = !muted));
  }

  async start(): Promise<void> {
    this.finished = false;
    this.generation++;
    this.onState("connecting");
    try {
      this.stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
          channelCount: 1,
        },
      });
    } catch {
      this.finish("error", "Microphone permission was denied.");
      return;
    }
    const scheme = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${scheme}://${location.host}/ws`);
    ws.binaryType = "arraybuffer";
    this.ws = ws;
    ws.onmessage = (event) => {
      if (typeof event.data === "string") {
        void this.onControl(JSON.parse(event.data));
      } else if (this.playback && this.upsampler) {
        const samples = this.upsampler.process(int16ToFloat(new Int16Array(event.data)));
        this.playback.port.postMessage(samples, [samples.buffer]);
      }
    };
    ws.onerror = () => this.finish("error", "Could not connect to the server.");
    ws.onclose = () => {
      if (this.ws === ws) this.finish("idle");
    };
  }

  stop(): void {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify({ type: "stop" }));
    }
    this.finish("idle");
  }

  private async onControl(message: { type: string; reason?: string }): Promise<void> {
    if (message.type === "ready") {
      await this.startAudio();
    } else if (message.type === "ringing") {
      this.onState("ringing");
    } else if (message.type === "busy") {
      this.finish("busy", "The intercom is in use by another client.");
    } else if (message.type === "rejected") {
      this.finish("error", ERROR_TEXT[message.reason ?? ""] ?? "Call ended.");
    } else if (message.type === "error") {
      this.finish("error", ERROR_TEXT[message.reason ?? ""] ?? "Session error.");
    }
  }

  private async startAudio(): Promise<void> {
    if (!this.stream || !this.ws || this.finished) return;
    const ws = this.ws;
    const generation = this.generation;
    const stale = () => this.finished || this.generation !== generation;
    const ctx = new AudioContext({ latencyHint: "interactive" });
    this.ctx = ctx;
    // finish() may run during any await below; it closes this.ctx, but if it ran before we
    // assigned it, or the session was replaced, we must close our own context.
    const abandon = () => void ctx.close().catch(() => {});
    try {
      await ctx.audioWorklet.addModule("capture-worklet.js");
      await ctx.audioWorklet.addModule("playback-worklet.js");
    } catch {
      abandon();
      if (!stale()) this.finish("error", "Could not load the audio processors.");
      return;
    }
    if (stale()) return abandon();

    const source = ctx.createMediaStreamSource(this.stream);
    const capture = new AudioWorkletNode(ctx, "capture");
    const mute = ctx.createGain();
    mute.gain.value = 0; // keeps the capture node pulled by the graph without local monitoring
    source.connect(capture);
    capture.connect(mute).connect(ctx.destination);

    const downsampler = new StreamResampler(ctx.sampleRate, WIRE_RATE);
    const framer = new Framer(FRAME_SAMPLES);
    capture.port.onmessage = (event) => {
      if (stale()) return;
      const pcm = floatToInt16(downsampler.process(event.data as Float32Array));
      for (const frame of framer.push(pcm)) {
        if (ws.readyState === WebSocket.OPEN) ws.send(frame);
      }
    };

    const playback = new AudioWorkletNode(ctx, "playback", { outputChannelCount: [1] });
    playback.connect(ctx.destination);
    this.playback = playback;
    this.upsampler = new StreamResampler(WIRE_RATE, ctx.sampleRate);
    try {
      await ctx.resume();
    } catch {
      abandon();
      if (!stale()) this.finish("error", "Could not start audio playback.");
      return;
    }
    if (stale()) return abandon();
    this.onState("live");
  }

  private finish(state: IntercomState, detail?: string): void {
    if (this.finished) return;
    this.finished = true;
    const ws = this.ws;
    this.ws = null;
    ws?.close();
    this.stream?.getTracks().forEach((track) => track.stop());
    void this.ctx?.close().catch(() => {});
    this.stream = this.ctx = this.playback = this.upsampler = null;
    this.onState(state, detail);
  }
}

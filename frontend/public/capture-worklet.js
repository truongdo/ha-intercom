// Forwards each 128-sample mono block to the main thread, which resamples and frames it.
class CaptureProcessor extends AudioWorkletProcessor {
  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (channel) this.port.postMessage(channel.slice());
    return true;
  }
}
registerProcessor("capture", CaptureProcessor);

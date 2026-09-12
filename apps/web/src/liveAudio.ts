export type AudioActivity = {
  input: number[];
  output: number[];
  inputLevel: number;
  outputLevel: number;
};

export const quietAudio: AudioActivity = {
  input: Array(9).fill(0),
  output: Array(9).fill(0),
  inputLevel: 0,
  outputLevel: 0,
};

type Channel = {
  source: MediaStreamAudioSourceNode;
  analyser: AnalyserNode;
  spectrum: Uint8Array<ArrayBuffer>;
  waveform: Float32Array<ArrayBuffer>;
};

export function createLiveAudioMeter(
  context: AudioContext,
  onActivity: (activity: AudioActivity) => void,
  isAudible: () => { input: boolean; output: boolean },
  onError: (message: string) => void,
) {
  const channels: Partial<Record<"input" | "output", Channel>> = {};
  const silence = context.createGain();
  silence.gain.value = 0;
  silence.connect(context.destination);
  let stopped = false;
  let previous = quietAudio;
  const resume = () => {
    if (context.state === "suspended")
      void context.resume().catch(() => {
        if (!stopped)
          onError(
            "Audio visualization is paused. Voice and captions are still available.",
          );
      });
  };
  resume();
  const disconnect = (key: "input" | "output") => {
    channels[key]?.source.disconnect();
    channels[key]?.analyser.disconnect();
    delete channels[key];
  };
  const attach = (key: "input" | "output", stream: MediaStream) => {
    disconnect(key);
    const source = context.createMediaStreamSource(stream);
    const analyser = context.createAnalyser();
    analyser.fftSize = 512;
    analyser.smoothingTimeConstant = 0.72;
    source.connect(analyser);
    // A silent destination keeps analysis running without playing the microphone
    // or duplicating the remote audio element.
    analyser.connect(silence);
    channels[key] = {
      source,
      analyser,
      spectrum: new Uint8Array(analyser.frequencyBinCount),
      waveform: new Float32Array(analyser.fftSize),
    };
  };
  const read = (key: "input" | "output", audible: boolean) => {
    const channel = channels[key];
    if (!channel || !audible || context.state !== "running")
      return { bands: quietAudio[key], level: 0 };
    channel.analyser.getByteFrequencyData(channel.spectrum);
    channel.analyser.getFloatTimeDomainData(channel.waveform);
    const energy = channel.waveform.reduce(
      (sum, sample) => sum + sample * sample,
      0,
    );
    const level = Math.min(1, Math.sqrt(energy / channel.waveform.length) * 5);
    const bands = Array.from({ length: 9 }, (_, band) => {
      const start = Math.floor(2 ** (band * 0.65));
      const end = Math.min(
        channel.spectrum.length,
        Math.floor(2 ** ((band + 1) * 0.65)) + 1,
      );
      let peak = 0;
      for (let index = start; index < end; index++)
        peak = Math.max(peak, channel.spectrum[index] / 255);
      return level < 0.015 ? 0 : peak;
    });
    return { bands, level };
  };
  const timer = setInterval(() => {
    const audible = isAudible();
    const input = read("input", audible.input);
    const output = read("output", audible.output);
    const next = {
      input: input.bands,
      output: output.bands,
      inputLevel: input.level,
      outputLevel: output.level,
    };
    if (
      Math.abs(previous.inputLevel - next.inputLevel) > 0.015 ||
      Math.abs(previous.outputLevel - next.outputLevel) > 0.015 ||
      next.input.some(
        (value, index) => Math.abs(value - previous.input[index]) > 0.035,
      ) ||
      next.output.some(
        (value, index) => Math.abs(value - previous.output[index]) > 0.035,
      ) ||
      (next.inputLevel === 0 && previous.inputLevel !== 0) ||
      (next.outputLevel === 0 && previous.outputLevel !== 0)
    ) {
      previous = next;
      onActivity(next);
    }
  }, 80);
  return {
    attach,
    resume,
    close() {
      stopped = true;
      clearInterval(timer);
      disconnect("input");
      disconnect("output");
      silence.disconnect();
      void context
        .close()
        .catch(() => onError("Audio visualization could not finish cleanup."));
    },
  };
}

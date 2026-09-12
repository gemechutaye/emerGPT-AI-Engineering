import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createLiveAudioMeter } from "../liveAudio";
import { transcriptTurns } from "../liveTranscript";

describe("Continuous transcript presentation", () => {
  it("orders late fragments across speakers and retains interruption and repeated words", () => {
    const turns = transcriptTurns([
      {
        id: "4",
        speaker: "input",
        text: "No, March.",
        start: 2500,
        end: 2800,
        order: 3,
      },
      {
        id: "1",
        speaker: "input",
        text: "Compare ",
        start: 0,
        end: 100,
        order: 0,
      },
      {
        id: "3",
        speaker: "output",
        text: "Let me check.",
        start: 1000,
        end: 2000,
        order: 2,
      },
      {
        id: "2",
        speaker: "input",
        text: "March. March.",
        start: 100,
        end: 300,
        order: 1,
      },
    ]);
    expect(turns.map(({ speaker, text }) => ({ speaker, text }))).toEqual([
      { speaker: "input", text: "Compare March. March." },
      { speaker: "output", text: "Let me check." },
      { speaker: "input", text: "No, March." },
    ]);
  });

  it("keeps a later utterance distinct after a pause, even from the same speaker", () => {
    expect(
      transcriptTurns([
        {
          id: "1",
          speaker: "input",
          text: "First question.",
          start: 0,
          end: 100,
          order: 0,
        },
        {
          id: "2",
          speaker: "input",
          text: "Next question.",
          start: 4000,
          end: 5000,
          order: 1,
        },
      ]),
    ).toHaveLength(2);
  });
});

describe("Audio activity presentation with declared Web Audio doubles", () => {
  let analyses: { disconnect: ReturnType<typeof vi.fn> }[];
  let sources: { disconnect: ReturnType<typeof vi.fn> }[];
  let close: ReturnType<typeof vi.fn>;
  beforeEach(() => {
    vi.useFakeTimers();
    analyses = [];
    sources = [];
    close = vi.fn(async () => undefined);
    vi.stubGlobal("MediaStream", class {});
    vi.stubGlobal(
      "AudioContext",
      class {
        state = "running";
        destination = {};
        resume = vi.fn(async () => undefined);
        close = close;
        createGain() {
          return { gain: { value: 1 }, connect: vi.fn(), disconnect: vi.fn() };
        }
        createMediaStreamSource() {
          const source = { connect: vi.fn(), disconnect: vi.fn() };
          sources.push(source);
          return source;
        }
        createAnalyser() {
          const level = analyses.length === 0 ? 0.08 : 0.14;
          const analyser = {
            fftSize: 512,
            smoothingTimeConstant: 0,
            frequencyBinCount: 256,
            connect: vi.fn(),
            disconnect: vi.fn(),
            getByteFrequencyData(data: Uint8Array) {
              data.fill(180);
            },
            getFloatTimeDomainData(data: Float32Array) {
              data.fill(level);
            },
          };
          analyses.push(analyser);
          return analyser;
        }
      },
    );
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("meters both streams, suppresses muted/blocked channels, and releases all analyser resources", () => {
    const update = vi.fn();
    const permissions = { input: true, output: true };
    const meter = createLiveAudioMeter(
      new AudioContext(),
      update,
      () => permissions,
      vi.fn(),
    );
    meter.attach("input", new MediaStream());
    meter.attach("output", new MediaStream());
    vi.advanceTimersByTime(80);
    const activity = update.mock.lastCall![0];
    expect(activity.inputLevel).toBeCloseTo(0.4);
    expect(activity.outputLevel).toBeCloseTo(0.7);
    expect(activity.input).toHaveLength(9);
    expect(activity.output.some((value: number) => value > 0)).toBe(true);
    permissions.input = false;
    permissions.output = false;
    vi.advanceTimersByTime(80);
    expect(update.mock.lastCall![0].inputLevel).toBe(0);
    expect(update.mock.lastCall![0].output).toEqual(Array(9).fill(0));
    meter.close();
    const calls = update.mock.calls.length;
    vi.advanceTimersByTime(1000);
    expect(update).toHaveBeenCalledTimes(calls);
    expect(close).toHaveBeenCalledOnce();
    expect(
      sources.every((source) => source.disconnect.mock.calls.length === 1),
    ).toBe(true);
    expect(
      analyses.every((analyser) => analyser.disconnect.mock.calls.length === 1),
    ).toBe(true);
  });
});

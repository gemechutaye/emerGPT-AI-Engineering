import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { mountParty } from "../../public/assets/home-disco/homepage-party.mjs";
import { DiscoMotor } from "../../public/assets/home-disco/disco-motion.mjs";

let dispose: (() => void) | undefined;
let controls: {
  ball: HTMLButtonElement;
  sound: HTMLButtonElement;
  status: HTMLParagraphElement;
};
let contexts: FakeAudio[];
class FakeAudio {
  currentTime = 0;
  state = "running";
  destination = {};
  close = vi.fn(async () => {
    this.state = "closed";
  });
  resume = vi.fn(async () => {});
  decodeAudioData = vi.fn(async () => ({ duration: 28 }));
  source = {
    connect: vi.fn(),
    disconnect: vi.fn(),
    start: vi.fn(),
    stop: vi.fn(),
    loopStart: 0,
    loopEnd: 0,
  };
  constructor() {
    contexts.push(this);
  }
  createGain() {
    return { gain: { value: 0 }, connect: vi.fn() };
  }
  createBufferSource() {
    return this.source;
  }
}
const settle = async () => {
  for (let i = 0; i < 12; i++) await Promise.resolve();
};
beforeEach(() => {
  contexts = [];
  vi.stubGlobal("AudioContext", FakeAudio);
  vi.stubGlobal("matchMedia", () =>
    Object.assign(new EventTarget(), { matches: true }),
  );
  vi.spyOn(document, "hidden", "get").mockReturnValue(false);
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => ({
      ok: true,
      json: async () => null,
      arrayBuffer: async () => new ArrayBuffer(0),
    })),
  );
  document.body.innerHTML =
    '<div><button id="ball"><img src="/assets/home-disco/disco-ball.png" /></button><button id="sound">Tap for sound</button><p role="status"></p></div>';
  controls = {
    ball: document.querySelector("#ball")!,
    sound: document.querySelector("#sound")!,
    status: document.querySelector("[role=status]")!,
  };
  dispose = mountParty(controls.ball, controls.sound, controls.status);
});
afterEach(() => {
  dispose?.();
  document.body.innerHTML = "";
});
it("waits for a gesture, loops the original recording and closes audio on mute", async () => {
  expect(contexts).toHaveLength(0);
  controls.sound.click();
  await settle();
  expect(controls.sound.textContent).toBe("Sound on");
  expect(controls.sound.getAttribute("aria-pressed")).toBe("true");
  expect(contexts[0].source.loopStart).toBe(0.65);
  expect(contexts[0].source.loopEnd).toBe(25.95);
  expect(contexts[0].source.start).toHaveBeenCalledOnce();
  controls.sound.click();
  expect(contexts[0].source.stop).toHaveBeenCalledOnce();
  expect(contexts[0].close).toHaveBeenCalledOnce();
  expect(controls.sound.textContent).toBe("Sound off");
  controls.ball.click();
  await settle();
  expect(contexts).toHaveLength(1); // A later ball tap must respect mute.
});
it("unmount stops playing audio and removes old gesture listeners", async () => {
  controls.sound.click();
  await settle();
  dispose?.();
  dispose = undefined;
  expect(contexts[0].close).toHaveBeenCalledOnce();
  expect(contexts[0].source.stop).toHaveBeenCalledOnce();
  controls.sound.click();
  controls.ball.click();
  await settle();
  expect(contexts).toHaveLength(1);
});
it("does not restart audio when an in-flight decode finishes after unmount", async () => {
  let resolve: (value: ArrayBuffer) => void = () => {};
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => ({
      ok: true,
      json: async () => null,
      arrayBuffer: () =>
        url.endsWith(".mp3")
          ? new Promise<ArrayBuffer>((r) => {
              resolve = r;
            })
          : Promise.resolve(new ArrayBuffer(0)),
    })),
  );
  controls.sound.click();
  await settle();
  dispose?.();
  dispose = undefined;
  resolve(new ArrayBuffer(0));
  await settle();
  expect(contexts[0].source.start).not.toHaveBeenCalled();
  expect(contexts[0].close).toHaveBeenCalledOnce();
});
it("keeps a still ball and emits no confetti under reduced motion", async () => {
  controls.ball.click();
  await settle();
  expect(document.querySelectorAll("video, canvas")).toHaveLength(0);
});
it("provides a retry after failed audio without leaking a context", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => ({ ok: false, json: async () => null })),
  );
  controls.sound.click();
  await settle();
  expect(controls.sound.textContent).toBe("Retry sound");
  expect(contexts[0].close).toHaveBeenCalledOnce();
  expect(controls.status.textContent).toContain("could not load");
});
it("preserves the original reverse fling, friction and cruise reset", () => {
  const motor = new DiscoMotor(7.2);
  motor.boost(-1);
  expect(motor.speed).toBeLessThan(0);
  motor.advance(0.1);
  expect(motor.angle).toBeLessThan(0);
  motor.advance(20);
  expect(motor.speed).toBeCloseTo(motor.rest, 4);
  motor.grab(100, 0, 300);
  motor.move(200, 50);
  expect(motor.release(60)).toBe(true);
  expect(motor.speed).toBeGreaterThan(motor.rest);
  motor.reset();
  expect(motor.speed).toBe(motor.rest);
});

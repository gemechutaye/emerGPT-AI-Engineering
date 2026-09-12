import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useLive } from "../useLive";

// Browser-media/provider doubles exercise cleanup and control boundaries only.
// They are not microphone, heard-audio, or natural-conversation acceptance.
const track = {
  stop: vi.fn(),
  enabled: true,
  onended: null as (() => void) | null,
};
const stream = { getTracks: () => [track], getAudioTracks: () => [track] };
class PeerDouble {
  static latest: PeerDouble;
  ontrack: ((event: { streams: unknown[]; track: unknown }) => void) | null =
    null;
  onconnectionstatechange: (() => void) | null = null;
  connectionState = "connected";
  channel = {
    onmessage: null as ((event: { data: string }) => void) | null,
    onclose: null as (() => void) | null,
    onerror: null as (() => void) | null,
  };
  close = vi.fn(() => {
    this.connectionState = "closed";
    this.onconnectionstatechange?.();
  });
  constructor() {
    PeerDouble.latest = this;
  }
  createDataChannel() {
    return this.channel;
  }
  addTrack() {}
  async createOffer() {
    return { sdp: "synthetic-offer", type: "offer" };
  }
  async setLocalDescription() {}
  async setRemoteDescription() {}
}
let status: {
  controller_connected: boolean;
  playback_blocked: boolean;
  status: string;
  last_run_id?: string;
};
let fetchDouble: ReturnType<typeof vi.fn>;
let getMicrophone: ReturnType<typeof vi.fn>;
let failStatus = false;
const response = (value: unknown) =>
  new Response(JSON.stringify(value), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });

beforeEach(() => {
  vi.useFakeTimers();
  track.stop.mockClear();
  track.enabled = true;
  failStatus = false;
  status = {
    controller_connected: true,
    playback_blocked: false,
    status: "active",
  };
  getMicrophone = vi.fn(async () => stream);
  Object.defineProperty(navigator, "mediaDevices", {
    configurable: true,
    value: { getUserMedia: getMicrophone },
  });
  vi.stubGlobal("isSecureContext", true);
  vi.stubGlobal("RTCPeerConnection", PeerDouble);
  vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue();
  vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(
    () => undefined,
  );
  fetchDouble = vi.fn(async (input: string) => {
    if (String(input).endsWith("/close")) return response({ status: "closed" });
    if (String(input).endsWith("/voice/sessions/live-1")) {
      if (failStatus) throw new Error("Connection failed");
      return response({ id: "live-1", ...status });
    }
    if (String(input).endsWith("/voice/sessions"))
      return response({
        id: "live-1",
        sdp: "synthetic-answer",
        max_duration_seconds: 300,
        status: "connecting",
      });
    throw new Error("Unexpected test request");
  });
  vi.stubGlobal("fetch", fetchDouble);
});
afterEach(() => {
  vi.useRealTimers();
});

describe("Live lifecycle with media and provider doubles", () => {
  it("reflects server-confirmed lookup work without disabling End", async () => {
    const { result } = renderHook(() => useLive(vi.fn()));
    status.status = "working";
    await act(() => result.current.start("conversation-1", 1));
    expect(result.current.state).toBe("working");
    expect(result.current.active).toBe(true);
    await act(() => result.current.end());
    expect(track.stop).toHaveBeenCalledOnce();
  });

  it("delivers a run first observed in the End response after stopping local media", async () => {
    const originalFetch = fetchDouble.getMockImplementation()!;
    fetchDouble.mockImplementation(async (input: string) =>
      String(input).endsWith("/close")
        ? response({ status: "closed", last_run_id: "end-race-run" })
        : originalFetch(input),
    );
    const onRun = vi.fn();
    const { result } = renderHook(() => useLive(onRun));
    await act(() => result.current.start("conversation-1", 1));
    await act(() => result.current.end());
    expect(onRun).toHaveBeenCalledWith("end-race-run");
    expect(track.stop).toHaveBeenCalledOnce();
  });

  it.each([
    "peer-disconnected",
    "channel-close",
    "channel-error",
    "provider-error",
    "provider-closed",
    "microphone-ended",
  ])(
    "ignores late %s after explicit End while server cleanup is pending",
    async (lateEvent) => {
      let closeResponse!: (value: Response) => void;
      const originalFetch = fetchDouble.getMockImplementation()!;
      fetchDouble.mockImplementation((input: string) =>
        String(input).endsWith("/close")
          ? new Promise<Response>((resolve) => {
              closeResponse = resolve;
            })
          : originalFetch(input),
      );
      const onRun = vi.fn();
      const { result } = renderHook(() => useLive(onRun));
      await act(() => result.current.start("conversation-1", 1));
      const oldPeer = PeerDouble.latest;
      let stopping!: Promise<void>;
      act(() => {
        stopping = result.current.end();
      });
      expect(result.current.state).toBe("ended");
      expect(result.current.active).toBe(false);
      expect(document.querySelector("audio")).toBeNull();
      act(() => {
        if (lateEvent === "peer-disconnected") {
          oldPeer.connectionState = "disconnected";
          oldPeer.onconnectionstatechange?.();
        } else if (lateEvent === "channel-close") oldPeer.channel.onclose?.();
        else if (lateEvent === "channel-error") oldPeer.channel.onerror?.();
        else if (lateEvent === "microphone-ended") track.onended?.();
        else
          oldPeer.channel.onmessage?.({
            data: JSON.stringify({
              type: lateEvent === "provider-error" ? "error" : "session.closed",
            }),
          });
        oldPeer.channel.onmessage?.({
          data: JSON.stringify({
            type: "session.input_transcript.delta",
            delta: "stale after End",
          }),
        });
      });
      expect(result.current.state).toBe("ended");
      expect(result.current.error).toBe("");
      expect(result.current.captions.input).toBe("");
      expect(track.stop).toHaveBeenCalledOnce();
      expect(
        fetchDouble.mock.calls.filter(([path]) =>
          String(path).endsWith("/close"),
        ),
      ).toHaveLength(1);
      await act(async () => {
        closeResponse(
          response({
            status: "closed",
            close_reason: "user",
            last_run_id: "persisted-after-End",
          }),
        );
        await stopping;
      });
      expect(result.current.state).toBe("ended");
      expect(result.current.error).toBe("");
      expect(onRun).toHaveBeenCalledExactlyOnceWith("persisted-after-End");
    },
  );

  it("does not hide a backend answer failure behind a successful media close", async () => {
    const originalFetch = fetchDouble.getMockImplementation()!;
    fetchDouble.mockImplementation(async (input: string) =>
      String(input).endsWith("/close")
        ? response({ status: "closed", error: "provider_rate_limited" })
        : originalFetch(input),
    );
    const { result } = renderHook(() => useLive(vi.fn()));
    await act(() => result.current.start("conversation-1", 1));
    await act(() => result.current.end());
    expect(result.current.state).toBe("error");
    expect(result.current.error).toContain("answer service is busy");
    expect(track.stop).toHaveBeenCalledOnce();
    expect(result.current.active).toBe(false);
  });

  it("delivers a run from a terminal poll before closing the voice connection", async () => {
    const onRun = vi.fn();
    const { result } = renderHook(() => useLive(onRun));
    await act(() => result.current.start("conversation-1", 1));
    status.status = "closed";
    status.last_run_id = "terminal-run";
    await act(() => vi.advanceTimersByTimeAsync(1000));
    expect(onRun).toHaveBeenCalledWith("terminal-run");
    expect(result.current.state).toBe("ended");
  });

  it("stops microphone tracks and playback before waiting for backend close", async () => {
    const { result } = renderHook(() => useLive(vi.fn()));
    await act(() => result.current.start("conversation-1", 1));
    expect(result.current.state).toBe("listening");
    const audio = document.querySelector("audio")!;
    expect(audio.muted).toBe(false);
    await act(() => result.current.end());
    expect(track.stop).toHaveBeenCalledOnce();
    expect(audio.muted).toBe(true);
    expect(PeerDouble.latest.close).toHaveBeenCalledOnce();
    expect(document.querySelector("audio")).toBeNull();
    expect(
      fetchDouble.mock.calls.some(([path]) =>
        String(path).endsWith("/live-1/close"),
      ),
    ).toBe(true);
  });

  it("mutes revised factual playback and stops the session if its controller is lost", async () => {
    const { result } = renderHook(() => useLive(vi.fn()));
    await act(() => result.current.start("conversation-1", 1));
    const audio = document.querySelector("audio")!;
    status.playback_blocked = true;
    await act(() => vi.advanceTimersByTimeAsync(1000));
    expect(audio.muted).toBe(true);
    status.controller_connected = false;
    await act(() => vi.advanceTimersByTimeAsync(1000));
    expect(result.current.state).toBe("error");
    expect(track.stop).toHaveBeenCalledOnce();
    expect(result.current.error).toContain("source-checking connection");
  });

  it("reports unconfirmed server cleanup while keeping local media off", async () => {
    const originalFetch = fetchDouble.getMockImplementation()!;
    fetchDouble.mockImplementation(async (input: string) =>
      String(input).endsWith("/close")
        ? response({ status: "closing", error: "live_close_unconfirmed" })
        : originalFetch(input),
    );
    const { result } = renderHook(() => useLive(vi.fn()));
    await act(() => result.current.start("conversation-1", 1));
    await act(() => result.current.end());
    expect(track.stop).toHaveBeenCalledOnce();
    expect(document.querySelector("audio")).toBeNull();
    expect(result.current.state).toBe("ended");
    expect(result.current.error).toContain("Server cleanup is not confirmed");
  });

  it("fails closed when session status cannot be verified", async () => {
    const { result } = renderHook(() => useLive(vi.fn()));
    await act(() => result.current.start("conversation-1", 1));
    failStatus = true;
    await act(() => vi.advanceTimersByTimeAsync(1000));
    expect(result.current.state).toBe("error");
    expect(track.stop).toHaveBeenCalledOnce();
    expect(result.current.error).toContain("status could not be verified");
  });

  it("stops a late microphone grant after End without creating a provider session", async () => {
    let grant: (value: typeof stream) => void = () => undefined;
    getMicrophone.mockImplementation(
      () =>
        new Promise((resolve) => {
          grant = resolve;
        }),
    );
    const { result } = renderHook(() => useLive(vi.fn()));
    let pending: Promise<void>;
    act(() => {
      pending = result.current.start("conversation-1", 1);
    });
    await act(() => result.current.end());
    await act(async () => {
      grant(stream);
      await pending;
    });
    expect(track.stop).toHaveBeenCalledOnce();
    expect(fetchDouble).not.toHaveBeenCalled();
    expect(result.current.state).toBe("ended");
  });

  it("uses a new offer identity after a recorded creation failure", async () => {
    const originalFetch = fetchDouble.getMockImplementation()!;
    const offers: { sdp: string; idempotency_key: string }[] = [];
    vi.spyOn(PeerDouble.prototype, "createOffer")
      .mockResolvedValueOnce({ sdp: "first-offer", type: "offer" })
      .mockResolvedValueOnce({ sdp: "second-offer", type: "offer" });
    fetchDouble.mockImplementation(
      async (input: string, options?: RequestInit) => {
        if (String(input).endsWith("/voice/sessions")) {
          offers.push(JSON.parse(String(options?.body)));
          if (offers.length === 1)
            return new Response(
              JSON.stringify({
                detail: {
                  code: "LIVE_CREATION_FAILED",
                  message: "Recorded failure",
                },
              }),
              { status: 503 },
            );
          if (offers[1].idempotency_key === offers[0].idempotency_key)
            return new Response(
              JSON.stringify({
                detail: {
                  code: "IDEMPOTENCY_CONFLICT",
                  message: "Different audio offer",
                },
              }),
              { status: 409 },
            );
        }
        return originalFetch(input);
      },
    );
    const { result } = renderHook(() => useLive(vi.fn()));
    await act(() => result.current.start("conversation-1", 1));
    expect(result.current.state).toBe("error");
    await act(() => result.current.start("conversation-1", 1));
    expect(result.current.state).toBe("listening");
    expect(offers[1].idempotency_key).not.toBe(offers[0].idempotency_key);
    await act(() => result.current.end());
  });

  it("does not create a session when End happens while the audio offer is being prepared", async () => {
    let prepared!: () => void;
    const localDescription = vi
      .spyOn(PeerDouble.prototype, "setLocalDescription")
      .mockImplementation(
        () =>
          new Promise<void>((resolve) => {
            prepared = resolve;
          }),
      );
    const { result } = renderHook(() => useLive(vi.fn()));
    let pending!: Promise<void>;
    await act(async () => {
      pending = result.current.start("conversation-1", 1);
    });
    expect(localDescription).toHaveBeenCalledOnce();
    await act(() => result.current.end());
    await act(async () => {
      prepared();
      await pending;
    });
    expect(fetchDouble).not.toHaveBeenCalled();
    expect(track.stop).toHaveBeenCalledOnce();
    expect(result.current.state).toBe("ended");
  });

  it("renders transcript events without treating delegation metadata as a question", async () => {
    const onRun = vi.fn();
    const { result } = renderHook(() => useLive(onRun));
    await act(() => result.current.start("conversation-1", 1));
    act(() => {
      PeerDouble.latest.channel.onmessage?.({
        data: JSON.stringify({
          type: "session.delegation.created",
          event_id: "event-1",
          text: "Not an authoritative utterance",
        }),
      });
      PeerDouble.latest.channel.onmessage?.({
        data: JSON.stringify({
          type: "session.input_transcript.delta",
          event_id: "event-2",
          delta: "What about PT-006?",
        }),
      });
      PeerDouble.latest.channel.onmessage?.({
        data: JSON.stringify({
          type: "session.input_transcript.delta",
          event_id: "event-2",
          delta: "What about PT-006?",
        }),
      });
    });
    expect(result.current.captions.input).toBe("What about PT-006?");
    expect(onRun).not.toHaveBeenCalled();
    status.last_run_id = "server-authorized-run";
    await act(() => vi.advanceTimersByTimeAsync(1000));
    expect(onRun).toHaveBeenCalledExactlyOnceWith("server-authorized-run");
  });
  it("serializes Start while microphone permission is unresolved and releases its caller on End", async () => {
    let grant!: (value: typeof stream) => void;
    getMicrophone.mockImplementation(
      () =>
        new Promise((resolve) => {
          grant = resolve;
        }),
    );
    const { result } = renderHook(() => useLive(vi.fn()));
    let settled = false;
    act(() => {
      void result.current.start("conversation-1", 1).then(() => {
        settled = true;
      });
      void result.current.start("conversation-1", 1);
    });
    expect(getMicrophone).toHaveBeenCalledOnce();
    await act(() => result.current.end());
    expect(settled).toBe(true);
    expect(result.current.active).toBe(false);
    await act(async () => {
      grant(stream);
    });
    expect(track.stop).toHaveBeenCalledOnce();
    expect(fetchDouble).not.toHaveBeenCalled();
  });

  it("bounds connecting even when microphone permission never resolves", async () => {
    getMicrophone.mockImplementation(() => new Promise(() => {}));
    const { result } = renderHook(() => useLive(vi.fn()));
    let settled = false;
    act(() => {
      void result.current.start("conversation-1", 1).then(() => {
        settled = true;
      });
    });
    await act(() => vi.advanceTimersByTimeAsync(45000));
    expect(result.current.state).toBe("error");
    expect(result.current.error).toContain("could not finish connecting");
    expect(settled).toBe(true);
    expect(fetchDouble).not.toHaveBeenCalled();
  });

  it("closes a late-created session after End without attaching its audio", async () => {
    let created!: (value: Response) => void;
    const originalFetch = fetchDouble.getMockImplementation()!;
    fetchDouble.mockImplementation((input: string) =>
      String(input).endsWith("/voice/sessions")
        ? new Promise((resolve) => {
            created = resolve;
          })
        : originalFetch(input),
    );
    const attach = vi.spyOn(PeerDouble.prototype, "setRemoteDescription");
    const { result } = renderHook(() => useLive(vi.fn()));
    let settled = false;
    await act(async () => {
      void result.current.start("conversation-1", 1).then(() => {
        settled = true;
      });
    });
    await act(() => result.current.end());
    expect(settled).toBe(true);
    await act(async () => {
      created(response({ id: "late-session", sdp: "late-answer" }));
    });
    expect(attach).not.toHaveBeenCalled();
    expect(
      fetchDouble.mock.calls.some(([path]) =>
        String(path).endsWith("/late-session/close"),
      ),
    ).toBe(true);
    expect(document.querySelector("audio")).toBeNull();
  });

  it("ignores old playback rejection and transcript callbacks after reconnect", async () => {
    let rejectPlayback!: (reason: Error) => void;
    vi.mocked(HTMLMediaElement.prototype.play).mockImplementationOnce(
      () =>
        new Promise((_, reject) => {
          rejectPlayback = reject;
        }),
    );
    const { result } = renderHook(() => useLive(vi.fn()));
    await act(() => result.current.start("conversation-1", 1));
    const oldPeer = PeerDouble.latest;
    act(() => oldPeer.ontrack?.({ streams: [stream], track }));
    await act(() => result.current.end());
    await act(() => result.current.start("conversation-1", 1));
    await act(async () => {
      rejectPlayback(new Error("late autoplay rejection"));
      oldPeer.channel.onmessage?.({
        data: JSON.stringify({
          type: "session.input_transcript.delta",
          delta: "stale",
        }),
      });
    });
    expect(result.current.needsPlayback).toBe(false);
    expect(result.current.captions.input).toBe("");
    expect(result.current.state).toBe("listening");
  });

  it("does not mark a connected controller as listening before the media peer connects", async () => {
    vi.spyOn(PeerDouble.prototype, "setRemoteDescription").mockImplementation(
      async () => {
        PeerDouble.latest.connectionState = "connecting";
      },
    );
    const { result } = renderHook(() => useLive(vi.fn()));
    await act(() => result.current.start("conversation-1", 1));
    act(() =>
      PeerDouble.latest.channel.onmessage?.({
        data: JSON.stringify({ type: "session.started" }),
      }),
    );
    expect(result.current.state).toBe("connecting");
    await act(() => vi.advanceTimersByTimeAsync(45000));
    expect(result.current.state).toBe("error");
    expect(track.stop).toHaveBeenCalledOnce();
  });
  it("detaches blocked audio and attaches only the current remote stream when checked playback resumes", async () => {
    const { result } = renderHook(() => useLive(vi.fn()));
    await act(() => result.current.start("conversation-1", 1));
    const audio = document.querySelector("audio")!;
    act(() => PeerDouble.latest.ontrack?.({ streams: [stream], track }));
    expect(audio.srcObject).toBe(stream);
    status.playback_blocked = true;
    await act(() => vi.advanceTimersByTimeAsync(1000));
    expect(audio.muted).toBe(true);
    expect(audio.srcObject).toBeNull();
    const currentStream = { getTracks: () => [track] };
    act(() => PeerDouble.latest.ontrack?.({ streams: [currentStream], track }));
    expect(audio.srcObject).toBeNull();
    status.playback_blocked = false;
    await act(() => vi.advanceTimersByTimeAsync(1000));
    expect(audio.srcObject).toBe(currentStream);
    expect(audio.muted).toBe(false);
  });

  it.each(["onclose", "onerror"] as const)(
    "stops local media when its data channel emits %s",
    async (event) => {
      const { result } = renderHook(() => useLive(vi.fn()));
      await act(() => result.current.start("conversation-1", 1));
      await act(async () => {
        PeerDouble.latest.channel[event]?.();
      });
      expect(result.current.state).toBe("error");
      expect(result.current.error).toContain("event connection");
      expect(track.stop).toHaveBeenCalledOnce();
      expect(document.querySelector("audio")).toBeNull();
    },
  );

  it("stops Live if the microphone ends unexpectedly", async () => {
    const { result } = renderHook(() => useLive(vi.fn()));
    await act(() => result.current.start("conversation-1", 1));
    await act(async () => {
      track.onended?.();
    });
    expect(result.current.state).toBe("error");
    expect(result.current.error).toContain("microphone disconnected");
    expect(document.querySelector("audio")).toBeNull();
  });

  it("places late caption fragments on their timeline while preserving repeated words and separate speakers", async () => {
    const onRun = vi.fn();
    const { result } = renderHook(() => useLive(onRun));
    await act(() => result.current.start("conversation-1", 1));
    act(() => {
      for (const message of [
        {
          type: "session.input_transcript.delta",
          event_id: "user-b",
          delta: "PT-005",
          start_ms: 200,
          end_ms: 300,
        },
        {
          type: "session.output_transcript.delta",
          event_id: "assistant-a",
          delta: "Checking.",
          start_ms: 100,
          end_ms: 250,
        },
        {
          type: "session.input_transcript.delta",
          event_id: "user-a",
          delta: "I meant ",
          start_ms: 100,
          end_ms: 200,
        },
        {
          type: "session.input_transcript.delta",
          event_id: "user-b",
          delta: "PT-005",
          start_ms: 200,
          end_ms: 300,
        },
        {
          type: "session.input_transcript.delta",
          event_id: "user-c",
          delta: " PT-005",
          start_ms: 400,
          end_ms: 500,
        },
      ])
        PeerDouble.latest.channel.onmessage?.({
          data: JSON.stringify(message),
        });
    });
    expect(result.current.captions).toEqual({
      input: "I meant PT-005 PT-005",
      output: "Checking.",
    });
    expect(onRun).not.toHaveBeenCalled();
  });

  it("clears the startup deadline once media and controller connect and keeps a healthy session active", async () => {
    vi.spyOn(PeerDouble.prototype, "setRemoteDescription").mockImplementation(
      async () => {
        PeerDouble.latest.connectionState = "connecting";
      },
    );
    const { result } = renderHook(() => useLive(vi.fn()));
    await act(() => result.current.start("conversation-1", 1));
    expect(result.current.state).toBe("connecting");
    await act(() => vi.advanceTimersByTimeAsync(5000));
    PeerDouble.latest.connectionState = "connected";
    await act(() => vi.advanceTimersByTimeAsync(1000));
    expect(result.current.state).toBe("listening");
    await act(() => vi.advanceTimersByTimeAsync(45000));
    expect(result.current.state).toBe("listening");
    expect(track.stop).not.toHaveBeenCalled();
  });
});

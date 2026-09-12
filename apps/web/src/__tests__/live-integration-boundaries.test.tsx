import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useLive } from "../useLive";

// Media and HTTP doubles verify playback permission, not physical audio timing.
const track = { stop: vi.fn(), enabled: true, onended: null };
const stream = { getTracks: () => [track], getAudioTracks: () => [track] };
class PeerDouble {
  static latest: PeerDouble;
  connectionState = "connected";
  ontrack: ((event: { streams: unknown[]; track: unknown }) => void) | null =
    null;
  onconnectionstatechange: (() => void) | null = null;
  channel = {
    onmessage: null as ((event: { data: string }) => void) | null,
    onclose: null,
    onerror: null,
  };
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
  close() {
    this.connectionState = "closed";
    this.onconnectionstatechange?.();
  }
}

type Snapshot = {
  id: string;
  status: string;
  controller_connected: boolean;
  playback_blocked: boolean;
  revision: number;
  last_delegation_id: string | null;
  last_run_id?: string;
};
let snapshot: Snapshot;
let nextStatus: Promise<Response> | undefined;
let fetchDouble: ReturnType<typeof vi.fn>;
const response = (value: unknown) =>
  new Response(JSON.stringify(value), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });

beforeEach(() => {
  vi.useFakeTimers();
  track.stop.mockClear();
  track.enabled = true;
  nextStatus = undefined;
  snapshot = {
    id: "live-1",
    status: "listening",
    controller_connected: true,
    playback_blocked: false,
    revision: 0,
    last_delegation_id: null,
  };
  Object.defineProperty(navigator, "mediaDevices", {
    configurable: true,
    value: { getUserMedia: vi.fn(async () => stream) },
  });
  vi.stubGlobal("isSecureContext", true);
  vi.stubGlobal("RTCPeerConnection", PeerDouble);
  vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue();
  vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {});
  fetchDouble = vi.fn(async (input: string) => {
    if (input.endsWith("/close"))
      return response({ ...snapshot, status: "closed" });
    if (input.endsWith("/voice/sessions/live-1")) {
      const deferred = nextStatus;
      nextStatus = undefined;
      return deferred ?? response(snapshot);
    }
    if (input.endsWith("/voice/sessions"))
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
afterEach(() => vi.useRealTimers());

function emitDelegation(id: string, offset: number, eventId = id) {
  act(() =>
    PeerDouble.latest.channel.onmessage?.({
      data: JSON.stringify({
        type: "session.delegation.created",
        event_id: eventId,
        offset_ms: offset,
        delegation: { id, target: "client" },
      }),
    }),
  );
}

function emitCaption(
  delta: string,
  start: number,
  end: number,
  speaker = "input",
) {
  act(() =>
    PeerDouble.latest.channel.onmessage?.({
      data: JSON.stringify({
        type: `session.${speaker}_transcript.delta`,
        event_id: `${speaker}-${start}-${end}-${delta}`,
        delta,
        start_ms: start,
        end_ms: end,
      }),
    }),
  );
}

async function start(onRun = vi.fn()) {
  const hook = renderHook(() => useLive(onRun));
  await act(() => hook.result.current.start("conversation-1", 3));
  act(() => PeerDouble.latest.ontrack?.({ streams: [stream], track }));
  return { ...hook, audio: document.querySelector("audio")!, onRun };
}

const poll = () => act(() => vi.advanceTimersByTimeAsync(1000));

async function checkedPlayback() {
  const hook = await start();
  emitDelegation("task-1", 100);
  snapshot = { ...snapshot, revision: 4, last_delegation_id: "task-1" };
  await poll();
  expect(hook.audio.muted).toBe(false);
  return hook;
}

it("requests automatic conversation context when starting Live", async () => {
  await start();
  const [, request] = fetchDouble.mock.calls.find(([path]) =>
    String(path).endsWith("/voice/sessions"),
  )!;
  expect(JSON.parse(request.body)).toMatchObject({
    conversation_id: "conversation-1",
    context_version: 3,
    automatic_context: true,
  });
});

it.each([
  { revision: 0, last_delegation_id: null },
  { revision: 1, last_delegation_id: "task-1" },
])(
  "blocks immediately and rejects an in-flight response from before delegation: %j",
  async (earlySnapshot) => {
    const { audio, result, onRun } = await start();
    expect(audio.srcObject).toBe(stream);
    let release!: (value: Response) => void;
    nextStatus = new Promise((resolve) => {
      release = resolve;
    });
    await poll();
    emitDelegation("task-1", 100);
    expect(audio.muted).toBe(true);
    expect(audio.srcObject).toBeNull();
    expect(result.current.state).toBe("working");

    await act(async () => {
      release(
        response({
          ...snapshot,
          ...earlySnapshot,
          last_run_id: "obsolete-run",
        }),
      );
    });
    expect(audio.muted).toBe(true);
    expect(audio.srcObject).toBeNull();
    expect(onRun).not.toHaveBeenCalled();
    // A fresh HTTP request can still see server state from before this delegation.
    await poll();
    expect(audio.muted).toBe(true);

    snapshot = {
      ...snapshot,
      revision: 1,
      last_delegation_id: "task-1",
      playback_blocked: true,
      status: "working",
    };
    await poll();
    expect(audio.srcObject).toBeNull();
    snapshot = {
      ...snapshot,
      playback_blocked: false,
      status: "listening",
      last_run_id: "checked-run",
    };
    await poll();
    expect(audio.muted).toBe(false);
    expect(audio.srcObject).toBe(stream);
    expect(onRun).toHaveBeenCalledExactlyOnceWith("checked-run");
  },
);

it("rejects an older revision even when its delegation identity matches", async () => {
  const { audio, onRun } = await start();
  emitDelegation("task-1", 100);
  snapshot = {
    ...snapshot,
    revision: 4,
    last_delegation_id: "task-1",
    playback_blocked: true,
    status: "working",
  };
  await poll();
  snapshot = {
    ...snapshot,
    revision: 3,
    playback_blocked: false,
    status: "listening",
    last_run_id: "old-revision-run",
  };
  await poll();
  expect(audio.muted).toBe(true);
  expect(audio.srcObject).toBeNull();
  expect(onRun).not.toHaveBeenCalled();
  snapshot = { ...snapshot, revision: 4, last_run_id: "corrected-run" };
  await poll();
  expect(audio.muted).toBe(false);
  expect(onRun).toHaveBeenCalledExactlyOnceWith("corrected-run");
});

it("recovers when the server processes a delegation before the browser receives it", async () => {
  snapshot = { ...snapshot, revision: 7, last_delegation_id: "task-7" };
  const { audio } = await start();
  emitDelegation("task-7", 700);
  expect(audio.muted).toBe(true);
  await poll();
  expect(audio.muted).toBe(false);
  expect(audio.srcObject).toBe(stream);
});

it("ignores duplicate and late older delegation events after checked playback resumes", async () => {
  const { audio, result } = await start();
  emitDelegation("task-2", 200);
  snapshot = { ...snapshot, revision: 2, last_delegation_id: "task-2" };
  await poll();
  expect(audio.muted).toBe(false);
  emitDelegation("task-2", 200);
  emitDelegation("task-2", 300, "duplicate-id-new-event");
  emitDelegation("task-1", 100);
  expect(audio.muted).toBe(false);
  expect(audio.srcObject).toBe(stream);
  expect(result.current.state).toBe("listening");
});

it("keeps End final when a pending matching checked snapshot arrives late", async () => {
  const { audio, result, onRun } = await start();
  emitDelegation("task-1", 100);
  let release!: (value: Response) => void;
  nextStatus = new Promise((resolve) => {
    release = resolve;
  });
  await poll();
  await act(() => result.current.end());
  expect(track.stop).toHaveBeenCalledOnce();
  await act(async () => {
    release(
      response({
        ...snapshot,
        revision: 1,
        last_delegation_id: "task-1",
        last_run_id: "late-run",
      }),
    );
  });
  expect(audio.muted).toBe(true);
  expect(audio.srcObject).toBeNull();
  expect(document.querySelector("audio")).toBeNull();
  expect(result.current.state).toBe("ended");
  expect(onRun).not.toHaveBeenCalled();
});

it("still stops media for a recovered terminal snapshot with an older revision", async () => {
  snapshot = { ...snapshot, revision: 4, last_delegation_id: "task-1" };
  const { result, audio } = await start();
  expect(audio.muted).toBe(false);
  snapshot = {
    ...snapshot,
    revision: 0,
    last_delegation_id: null,
    status: "failed",
    controller_connected: false,
    playback_blocked: true,
  };
  await poll();
  expect(result.current.state).toBe("error");
  expect(audio.muted).toBe(true);
  expect(audio.srcObject).toBeNull();
  expect(track.stop).toHaveBeenCalledOnce();
});

it("blocks a split correction immediately and fences old status until a fresh checked delegation", async () => {
  const { audio, result, onRun } = await checkedPlayback();
  let release!: (value: Response) => void;
  nextStatus = new Promise((resolve) => {
    release = resolve;
  });
  await poll();
  emitCaption("No", 200, 220);
  expect(audio.muted).toBe(false);
  emitCaption(", I", 220, 240);
  expect(audio.muted).toBe(true);
  expect(audio.srcObject).toBeNull();
  expect(result.current.state).toBe("working");
  const pauses = vi.mocked(audio.pause).mock.calls.length;
  emitCaption(" mean", 240, 260);
  expect(vi.mocked(audio.pause)).toHaveBeenCalledTimes(pauses);
  await act(async () => {
    release(response({ ...snapshot, last_run_id: "obsolete-run" }));
  });
  expect(onRun).not.toHaveBeenCalled();

  // A fresh poll can still return pre-correction listening state. Replayed
  // delegation IDs and older offsets do not resolve the pending correction.
  emitDelegation("task-1", 300, "duplicate-before-correction-settles");
  emitDelegation("task-0", 50);
  await poll();
  expect(audio.muted).toBe(true);
  expect(audio.srcObject).toBeNull();

  emitDelegation("task-2", 300);
  snapshot = { ...snapshot, last_delegation_id: "task-2" };
  await poll();
  expect(audio.muted).toBe(true); // Matching ID alone cannot reuse revision 4.
  snapshot = {
    ...snapshot,
    revision: 5,
    playback_blocked: true,
    status: "working",
  };
  await poll();
  expect(audio.srcObject).toBeNull();
  snapshot = {
    ...snapshot,
    playback_blocked: false,
    status: "listening",
    last_run_id: "corrected-run",
  };
  await poll();
  expect(audio.muted).toBe(false);
  expect(audio.srcObject).toBe(stream);
  expect(onRun).toHaveBeenCalledExactlyOnceWith("corrected-run");
});

it.each([false, true])(
  "recovers after a correction when the server sees the new delegation first (before cue: %s)",
  async (beforeCue) => {
    const { audio } = await checkedPlayback();
    if (!beforeCue) emitCaption("No,", 200, 250);
    snapshot = { ...snapshot, revision: 6, last_delegation_id: "task-2" };
    await poll();
    if (beforeCue) emitCaption("No,", 200, 250);
    expect(audio.muted).toBe(true);
    expect(audio.srcObject).toBeNull();
    emitDelegation("task-2", 300);
    await poll();
    expect(audio.muted).toBe(false);
    expect(audio.srcObject).toBe(stream);
  },
);

it.each([
  "No treatment is documented.",
  "There is no documented price.",
  "The procedure was not completed.",
  "No side effects were reported.",
])("leaves playback permitted for ordinary negation: %s", async (text) => {
  const { audio, result } = await checkedPlayback();
  emitCaption(text, 200, 300);
  await poll();
  expect(audio.muted).toBe(false);
  expect(audio.srcObject).toBe(stream);
  expect(result.current.state).toBe("listening");
});

it.each(["No,", "Actually.", "I meant"])(
  "lets a fresh delegation resolve the brief correction %s without waiting for more words",
  async (cue) => {
    const { audio } = await checkedPlayback();
    emitCaption(cue, 200, 250);
    expect(audio.muted).toBe(true);
    snapshot = { ...snapshot, revision: 5 };
    await poll();
    expect(audio.muted).toBe(true); // Even a newer old-delegation snapshot is insufficient.
    emitDelegation("task-2", 250);
    snapshot = { ...snapshot, revision: 6, last_delegation_id: "task-2" };
    await poll();
    expect(audio.muted).toBe(false);
  },
);

it("excludes old correction captions and assistant cues from the current user segment", async () => {
  const { audio } = await checkedPlayback();
  emitCaption("No, I mean", 200, 250);
  emitDelegation("task-2", 300);
  snapshot = { ...snapshot, revision: 6, last_delegation_id: "task-2" };
  await poll();
  expect(audio.muted).toBe(false);
  emitCaption("Actually", 260, 290); // Late historical delta remains a caption.
  emitCaption("Sorry,", 310, 330, "output");
  emitCaption("What is their follow-up?", 400, 500);
  await poll();
  expect(audio.muted).toBe(false);
  expect(audio.srcObject).toBe(stream);
  emitCaption(" Switch to patient six.", 510, 600);
  expect(audio.muted).toBe(true); // A fresh current correction still fences playback.
});

it("keeps End final during a correction with late status and delegation events", async () => {
  const { audio, result, onRun } = await checkedPlayback();
  emitCaption("No,", 200, 250);
  let release!: (value: Response) => void;
  nextStatus = new Promise((resolve) => {
    release = resolve;
  });
  await poll();
  await act(() => result.current.end());
  emitDelegation("task-2", 300);
  await act(async () => {
    release(
      response({
        ...snapshot,
        revision: 6,
        last_delegation_id: "task-2",
        last_run_id: "late-corrected-run",
      }),
    );
  });
  expect(track.stop).toHaveBeenCalledOnce();
  expect(result.current.state).toBe("ended");
  expect(result.current.needsPlayback).toBe(false);
  expect(audio.muted).toBe(true);
  expect(audio.srcObject).toBeNull();
  expect(document.querySelector("audio")).toBeNull();
  expect(onRun).not.toHaveBeenCalled();
});

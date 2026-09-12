import { act, renderHook } from "@testing-library/react";
import { QueryClient } from "@tanstack/react-query";
import { describe, expect, it, vi } from "vitest";
import { bootstrapSession, useHistorySync } from "../historySync";

describe("shared history", () => {
  it("serializes simultaneous first-tab bootstraps before the cookie is issued", async () => {
    let queue: Promise<unknown> = Promise.resolve();
    const request = vi.fn((_name, callback) => {
      const next = queue.then(callback);
      queue = next;
      return next;
    });
    const original = Object.getOwnPropertyDescriptor(navigator, "locks");
    Object.defineProperty(navigator, "locks", {
      configurable: true,
      value: { request },
    });
    let cookie: string | undefined;
    let ownersCreated = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        const id = cookie ?? `owner-${++ownersCreated}`;
        await Promise.resolve();
        cookie = id;
        return new Response(JSON.stringify({ id }));
      }),
    );
    try {
      const sessions = await Promise.all([
        bootstrapSession(),
        bootstrapSession(),
      ]);
      expect(sessions.map((session) => session.id)).toEqual([
        "owner-1",
        "owner-1",
      ]);
      expect(ownersCreated).toBe(1);
      expect(request).toHaveBeenCalledTimes(2);
    } finally {
      if (original) Object.defineProperty(navigator, "locks", original);
      else Reflect.deleteProperty(navigator, "locks");
    }
  });

  it("refreshes both history and the open conversation on server changes and focus", () => {
    const streams: (EventTarget & { close: ReturnType<typeof vi.fn> })[] = [];
    vi.stubGlobal(
      "EventSource",
      class extends EventTarget {
        close = vi.fn();
        constructor() {
          super();
          streams.push(this);
        }
      },
    );
    const client = new QueryClient();
    const invalidate = vi.spyOn(client, "invalidateQueries");
    const { unmount } = renderHook(() =>
      useHistorySync(client, "browser-owner"),
    );
    invalidate.mockClear();
    act(() => streams[0].dispatchEvent(new Event("changed")));
    expect(invalidate.mock.calls.map(([options]) => options?.queryKey)).toEqual(
      [
        ["browser-owner", "conversations"],
        ["browser-owner", "chat-search"],
        ["browser-owner", "conversation"],
        ["browser-owner", "voice-transcripts"],
      ],
    );
    invalidate.mockClear();
    act(() => window.dispatchEvent(new Event("focus")));
    expect(invalidate).toHaveBeenCalledTimes(4);
    const expired = vi.fn();
    window.addEventListener("emer:session-expired", expired);
    act(() => streams[0].dispatchEvent(new Event("expired")));
    expect(expired).toHaveBeenCalledOnce();
    window.removeEventListener("emer:session-expired", expired);
    unmount();
    expect(streams[0].close).toHaveBeenCalled();
  });
});

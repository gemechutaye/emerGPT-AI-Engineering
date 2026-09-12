import { useEffect } from "react";
import type { QueryClient } from "@tanstack/react-query";
import { post, type Session } from "./api";

// Serialize first visits across tabs so simultaneous cookie-less requests cannot
// create competing owners and overwrite the shared HttpOnly cookie.
export async function bootstrapSession(): Promise<Session> {
  const bootstrap = () => post<Session>("/session");
  return navigator.locks
    ? await navigator.locks.request("emer:session-bootstrap", bootstrap)
    : bootstrap();
}

export function useHistorySync(client: QueryClient, owner?: string) {
  useEffect(() => {
    if (!owner) return;
    const refresh = () => {
      for (const key of [
        "conversations",
        "chat-search",
        "conversation",
        "voice-transcripts",
      ])
        void client.invalidateQueries({ queryKey: [owner, key] });
    };
    let stream: EventSource | undefined;
    const connect = () => {
      stream?.close();
      stream = undefined;
      if (document.visibilityState === "hidden") return;
      refresh();
      if (typeof EventSource === "undefined") return;
      stream = new EventSource("/api/v1/history/events");
      stream.addEventListener("changed", refresh);
      stream.addEventListener("expired", () => {
        stream?.close();
        window.dispatchEvent(new Event("emer:session-expired"));
      });
    };
    connect();
    window.addEventListener("online", connect);
    window.addEventListener("focus", refresh);
    document.addEventListener("visibilitychange", connect);
    return () => {
      stream?.close();
      window.removeEventListener("online", connect);
      window.removeEventListener("focus", refresh);
      document.removeEventListener("visibilitychange", connect);
    };
  }, [client, owner]);
}

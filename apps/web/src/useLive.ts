import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, idempotencyKey, post } from "./api";
import {
  createLiveAudioMeter,
  quietAudio,
  type AudioActivity,
} from "./liveAudio";
import { transcriptTurns, type CaptionFragment } from "./liveTranscript";

// Keep explicit cues aligned with domain/utterance.py. Captions only remove
// playback permission; the checked server snapshot still owns recovery.
const correctionCue =
  /\b(?:actually|correction|i mean(?:t)?|sorry(?=[,\s])|instead|change that|switch to|change to)\b|\bno(?=\s*,)|(?:^|[.!?]\s*)no(?=\s+(?:patient\b|p[.\s-]*t\b|i mean))/i;

function liveFailure(error: LiveSnapshot["error"]): string | undefined {
  const message = typeof error === "string" ? error : error?.message;
  if (!message) return undefined;
  if (message === "provider_rate_limited")
    return "The answer service is busy. Try voice again shortly, or keep typing.";
  if (message === "provider_capacity_exhausted")
    return "The answer service has no available capacity. Voice is off; your chat is saved.";
  if (message === "live_delegation_failed")
    return "Voice couldn't complete that answer. Try again, or type your question.";
  if (message.includes(" ")) return message;
  return "Voice stopped unexpectedly. Your microphone is off. Try again.";
}

type LiveSnapshot = {
  id: string;
  status: string;
  controller_connected: boolean;
  playback_blocked: boolean;
  revision: number;
  last_delegation_id?: string | null;
  last_run_id?: string;
  usage_seconds?: number;
  error?: string | { message?: string };
};
type LiveState =
  | "idle"
  | "connecting"
  | "listening"
  | "working"
  | "ended"
  | "error";
type LiveResources = {
  starting?: boolean;
  cancelStart?: () => void;
  startupTimer?: ReturnType<typeof setTimeout>;
  peer?: RTCPeerConnection;
  stream?: MediaStream;
  audio?: HTMLAudioElement;
  id?: string;
  interval?: ReturnType<typeof setInterval>;
  timer?: ReturnType<typeof setTimeout>;
  meter?: ReturnType<typeof createLiveAudioMeter>;
};

export function useLive(onRun: (runId: string) => void) {
  const [state, setState] = useState<LiveState>("idle");
  const [error, setError] = useState("");
  const [fragments, setFragments] = useState<CaptionFragment[]>([]);
  const transcript = useMemo(() => transcriptTurns(fragments), [fragments]);
  const captions = useMemo(
    () => ({
      input: transcript
        .filter((turn) => turn.speaker === "input")
        .map((turn) => turn.text)
        .join("")
        .slice(-4000),
      output: transcript
        .filter((turn) => turn.speaker === "output")
        .map((turn) => turn.text)
        .join("")
        .slice(-4000),
    }),
    [transcript],
  );
  const [transcriptTruncated, setTranscriptTruncated] = useState(false);
  const [audioActivity, setAudioActivity] = useState<AudioActivity>(quietAudio);
  const [visualizerError, setVisualizerError] = useState("");
  const [muted, setMuted] = useState(false);
  const [needsPlayback, setNeedsPlayback] = useState(false);
  const resources = useRef<LiveResources>({});
  const generation = useRef(0);
  const onRunRef = useRef(onRun);
  onRunRef.current = onRun;

  const cleanup = useCallback(() => {
    const current = resources.current;
    current.cancelStart?.();
    clearTimeout(current.startupTimer);
    current.meter?.close();
    current.stream?.getTracks().forEach((track) => track.stop());
    if (current.audio) {
      current.audio.muted = true;
      current.audio.pause();
      current.audio.srcObject = null;
      current.audio.remove();
    }
    current.peer?.close();
    clearInterval(current.interval);
    clearTimeout(current.timer);
    resources.current = {};
    return current.id;
  }, []);

  const end = useCallback(
    async (failure?: string) => {
      const ownGeneration = ++generation.current;
      const id = cleanup();
      setMuted(false);
      setNeedsPlayback(false);
      setAudioActivity(quietAudio);
      setState(failure ? "error" : "ended");
      setError(failure ?? "");
      if (id) {
        try {
          const snapshot = await post<LiveSnapshot>(
            `/voice/sessions/${id}/close`,
          );
          if (generation.current === ownGeneration && snapshot.last_run_id)
            onRunRef.current(snapshot.last_run_id);
          if (
            generation.current === ownGeneration &&
            snapshot.error === "live_close_unconfirmed"
          ) {
            setError(
              "Your microphone and audio are off. Server cleanup is not confirmed; another voice session remains blocked until cleanup can be confirmed.",
            );
          } else if (generation.current === ownGeneration && snapshot.error) {
            setError(liveFailure(snapshot.error) ?? "");
            setState("error");
          }
        } catch {
          if (generation.current === ownGeneration)
            setError(
              failure ??
                "Your microphone and audio are off. The server could not confirm cleanup; its session timeout still applies.",
            );
        }
      }
    },
    [cleanup],
  );

  useEffect(
    () => () => {
      generation.current += 1;
      const id = cleanup();
      if (id) void post(`/voice/sessions/${id}/close`).catch(() => undefined);
    },
    [cleanup],
  );

  const start = useCallback(
    async (conversationId: string, contextVersion: number) => {
      if (
        resources.current.starting ||
        resources.current.peer ||
        resources.current.stream
      )
        return;
      const ownGeneration = ++generation.current;
      const isCurrent = () => generation.current === ownGeneration;
      resources.current.starting = true;
      const cancelled = new Promise<void>((resolve) => {
        resources.current.cancelStart = resolve;
      });
      resources.current.startupTimer = setTimeout(() => {
        if (isCurrent())
          void end(
            "Live could not finish connecting. Microphone and audio are off. Check microphone permission and your connection, then try again.",
          );
      }, 45000);
      setState("connecting");
      setError("");
      setFragments([]);
      setTranscriptTruncated(false);
      setAudioActivity(quietAudio);
      setVisualizerError("");
      setMuted(false);
      setNeedsPlayback(false);
      // Each peer prepares a different SDP offer, so it is a new connection
      // attempt. The server separately blocks active or uncertain sessions.
      const requestKey = idempotencyKey();
      const connect = async () => {
        if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia)
          throw new Error(
            "Live needs a secure connection and a browser with microphone support.",
          );
        const stream = await navigator.mediaDevices.getUserMedia({
          audio: {
            echoCancellation: true,
            noiseSuppression: true,
            autoGainControl: true,
          },
        });
        if (!isCurrent()) {
          stream.getTracks().forEach((track) => track.stop());
          return;
        }
        resources.current.stream = stream;
        const peer = new RTCPeerConnection();
        resources.current.peer = peer;
        const audio = document.createElement("audio");
        audio.autoplay = true;
        audio.muted = true;
        audio.style.display = "none";
        document.body.append(audio);
        resources.current.audio = audio;
        if (typeof AudioContext === "undefined") {
          setVisualizerError(
            "Audio visualization is unavailable in this browser. Voice and captions still work.",
          );
        } else {
          try {
            const meter = createLiveAudioMeter(
              new AudioContext(),
              (activity) => {
                if (isCurrent()) setAudioActivity(activity);
              },
              () => ({
                input: stream.getAudioTracks().some((track) => track.enabled),
                output: !audio.muted && !audio.paused && !!audio.srcObject,
              }),
              (message) => {
                if (isCurrent()) setVisualizerError(message);
              },
            );
            resources.current.meter = meter;
            meter.attach("input", stream);
          } catch {
            resources.current.meter?.close();
            resources.current.meter = undefined;
            setVisualizerError(
              "Audio visualization could not start. Voice and captions are still available.",
            );
          }
        }
        let remoteStream: MediaStream | undefined;
        let delegationEpoch = 0;
        let latestDelegationId: string | undefined;
        let latestDelegationOffset = -1;
        let latestSnapshotRevision = 0;
        let latestMatchingRevision = 0;
        let correctionPending = false;
        let correctionRevision: number | undefined;
        let correctionFragments: CaptionFragment[] = [];
        const seenDelegations = new Set<string>();
        const blockPlayback = () => {
          audio.muted = true;
          audio.pause();
          audio.srcObject = null;
          setNeedsPlayback(false);
        };
        const playCurrent = () => {
          if (!remoteStream || audio.muted || !isCurrent()) return;
          audio.srcObject = remoteStream;
          void audio.play().catch(() => {
            if (isCurrent() && audio.srcObject === remoteStream)
              setNeedsPlayback(true);
          });
        };
        peer.ontrack = (event) => {
          if (!isCurrent()) return;
          remoteStream = event.streams[0] ?? new MediaStream([event.track]);
          try {
            resources.current.meter?.attach("output", remoteStream);
          } catch {
            setVisualizerError(
              "Incoming audio visualization is unavailable. Voice and captions are still available.",
            );
          }
          playCurrent();
        };
        peer.onconnectionstatechange = () => {
          if (
            isCurrent() &&
            ["failed", "disconnected", "closed"].includes(peer.connectionState)
          )
            void end(
              "Live lost its connection. Audio and microphone are off. Start again when your connection is stable.",
            );
        };
        stream.getTracks().forEach((track) => {
          track.onended = () => {
            if (isCurrent())
              void end(
                "Your microphone disconnected. Live audio is off. Reconnect the microphone, then start again.",
              );
          };
          peer.addTrack(track, stream);
        });
        const channel = peer.createDataChannel("oai-events");
        const channelLost = () => {
          if (isCurrent())
            void end(
              "Live lost its event connection. Microphone and audio are off. Start again to reconnect.",
            );
        };
        channel.onclose = channelLost;
        channel.onerror = channelLost;
        let fragmentOrder = 0;
        let latestCaptionEnd = 0;
        const seen = new Set<string>();
        channel.onmessage = (event) => {
          if (!isCurrent() || typeof event.data !== "string") return;
          let message: Record<string, unknown>;
          try {
            message = JSON.parse(event.data);
          } catch {
            return;
          }
          const id =
            typeof message.event_id === "string" ? message.event_id : "";
          if (id && seen.has(id)) return;
          if (id) {
            seen.add(id);
            if (seen.size > 2000) seen.delete(seen.values().next().value!);
          }
          const type = String(message.type ?? "");
          if (type === "session.delegation.created") {
            const delegation = message.delegation as
              | { id?: unknown; target?: unknown }
              | undefined;
            const offset = message.offset_ms ?? 0;
            if (
              delegation?.target === "client" &&
              typeof delegation.id === "string" &&
              delegation.id.length > 0 &&
              typeof offset === "number" &&
              Number.isFinite(offset) &&
              offset >= 0 &&
              offset >= latestDelegationOffset &&
              !seenDelegations.has(delegation.id)
            ) {
              seenDelegations.add(delegation.id);
              latestDelegationId = delegation.id;
              latestDelegationOffset = offset;
              delegationEpoch += 1;
              correctionPending = false;
              correctionFragments = correctionFragments.filter(
                (fragment) => fragment.end > offset,
              );
              // A client event may remove playback permission immediately. Only
              // the matching checked server snapshot may restore it.
              blockPlayback();
              if (peer.connectionState === "connected") setState("working");
            }
          }
          if (
            type === "session.input_transcript.delta" ||
            type === "session.output_transcript.delta"
          ) {
            const key = type.includes("input_") ? "input" : "output";
            if (typeof message.delta === "string") {
              // These are provisional captions, never instructions or evidence.
              const order = fragmentOrder++;
              const start =
                typeof message.start_ms === "number" &&
                Number.isFinite(message.start_ms)
                  ? Math.max(0, message.start_ms)
                  : latestCaptionEnd;
              const end =
                typeof message.end_ms === "number" &&
                Number.isFinite(message.end_ms)
                  ? Math.max(start, message.end_ms)
                  : start;
              latestCaptionEnd = Math.max(latestCaptionEnd, end);
              const fragment: CaptionFragment = {
                id: id || `caption-${order}`,
                speaker: key,
                text: message.delta,
                start,
                end,
                order,
              };
              if (key === "input") {
                correctionFragments = [...correctionFragments, fragment]
                  .filter((part) => part.end > latestDelegationOffset)
                  .slice(-4000);
                if (
                  latestDelegationId !== undefined &&
                  end > latestDelegationOffset &&
                  !correctionPending &&
                  correctionCue.test(
                    [...correctionFragments]
                      .sort((a, b) => a.start - b.start || a.end - b.end)
                      .map((part) => part.text)
                      .join(""),
                  )
                ) {
                  // Coalesce split cues until a fresh provider delegation. A
                  // listening response for the old request cannot unmute us.
                  correctionPending = true;
                  correctionRevision = latestMatchingRevision;
                  delegationEpoch += 1;
                  blockPlayback();
                  if (peer.connectionState === "connected") setState("working");
                }
              }
              if (order >= 4000) setTranscriptTruncated(true);
              setFragments((previous) => [...previous, fragment].slice(-4000));
            }
          }
          if (type === "error" || type === "session.closed")
            void end(
              type === "error"
                ? "The voice service reported an error. Audio and microphone are off."
                : undefined,
            );
        };
        const offer = await peer.createOffer();
        if (!isCurrent()) return;
        await peer.setLocalDescription(offer);
        if (!isCurrent()) return;
        const created = await post<{
          id: string;
          sdp: string;
          status: string;
          max_duration_seconds: number;
        }>("/voice/sessions", {
          conversation_id: conversationId,
          context_version: contextVersion,
          automatic_context: true,
          sdp: offer.sdp,
          idempotency_key: requestKey,
        });
        if (!isCurrent()) {
          void post(`/voice/sessions/${created.id}/close`).catch(
            () => undefined,
          );
          return;
        }
        resources.current.id = created.id;
        await peer.setRemoteDescription({ type: "answer", sdp: created.sdp });
        if (!isCurrent()) return;
        let polling = false;
        let controlled = false;
        let lastRun = "";
        const createdAt = Date.now();
        const inspect = async () => {
          if (polling || !isCurrent()) return;
          polling = true;
          const inspectedEpoch = delegationEpoch;
          try {
            const snapshot = await api<LiveSnapshot>(
              `/voice/sessions/${created.id}`,
              { signal: AbortSignal.timeout(4000) },
            );
            if (!isCurrent()) return;
            const revision = snapshot.revision ?? 0;
            const obsolete =
              inspectedEpoch !== delegationEpoch ||
              revision < latestSnapshotRevision;
            const matchesDelegation =
              latestDelegationId === undefined ||
              snapshot.last_delegation_id === latestDelegationId;
            const matchesRequest =
              matchesDelegation &&
              !correctionPending &&
              (correctionRevision === undefined ||
                revision > correctionRevision);
            if (
              !obsolete &&
              matchesRequest &&
              snapshot.last_run_id &&
              snapshot.last_run_id !== lastRun
            ) {
              lastRun = snapshot.last_run_id;
              onRunRef.current(lastRun);
            }
            if (
              ["failed", "error", "closed", "expired", "ended"].includes(
                snapshot.status,
              )
            ) {
              const message = liveFailure(snapshot.error);
              await end(
                message ||
                  (snapshot.status === "failed"
                    ? "Live stopped because its control connection was lost."
                    : undefined),
              );
              return;
            }
            if (
              !snapshot.controller_connected &&
              (controlled || Date.now() - createdAt > 10000)
            ) {
              await end(
                "Live could not keep its source-checking connection. Audio and microphone are off.",
              );
              return;
            }
            // Recovery can publish a terminal/ownership-loss snapshot with an
            // older revision. It must still stop media, never reopen it.
            if (obsolete) return;
            latestSnapshotRevision = revision;
            // The server can process the next delegation before its client
            // event arrives. Its revision must not raise the correction floor.
            if (matchesDelegation) latestMatchingRevision = revision;
            if (snapshot.controller_connected) {
              controlled = true;
              if (peer.connectionState === "connected") {
                clearTimeout(resources.current.startupTimer);
                setState(
                  snapshot.status === "working" || !matchesRequest
                    ? "working"
                    : "listening",
                );
              }
            }
            audio.muted =
              !snapshot.controller_connected ||
              !matchesRequest ||
              snapshot.playback_blocked ||
              peer.connectionState !== "connected";
            if (audio.muted) {
              // Detach the rendered stream while blocked; do not retain a paused
              // playback source to resume when a corrected answer is ready.
              blockPlayback();
            } else if (audio.srcObject !== remoteStream) {
              playCurrent();
            }
          } catch {
            if (isCurrent())
              await end(
                "Live status could not be verified. Audio and microphone are off. Reconnect before starting again.",
              );
          } finally {
            polling = false;
          }
        };
        await inspect();
        if (!isCurrent()) return;
        resources.current.interval = setInterval(() => void inspect(), 1000);
        resources.current.timer = setTimeout(
          () => {
            if (isCurrent()) void end();
          },
          Math.min(created.max_duration_seconds || 300, 300) * 1000,
        );
      };
      try {
        // Permission and browser SDP promises cannot be aborted. End must still
        // release the caller immediately; connect fences every late result.
        await Promise.race([connect(), cancelled]);
      } catch (cause) {
        if (!isCurrent()) return;
        const message =
          cause instanceof DOMException && cause.name === "NotAllowedError"
            ? "Microphone access was denied. Allow it in your browser settings, then start Live again. You can keep typing here."
            : cause instanceof Error
              ? cause.message
              : "Live could not start. Please try again.";
        await end(message);
      }
    },
    [end],
  );

  const toggleMute = () => {
    setMuted((previous) => {
      resources.current.stream?.getAudioTracks().forEach((track) => {
        track.enabled = previous;
      });
      return !previous;
    });
  };
  const resumePlayback = async () => {
    const ownGeneration = generation.current;
    const audio = resources.current.audio;
    if (!audio?.srcObject || audio.muted) return;
    try {
      resources.current.meter?.resume();
      await audio.play();
      if (generation.current === ownGeneration) setNeedsPlayback(false);
    } catch {
      if (generation.current !== ownGeneration) return;
      setError(
        "Your browser is still blocking audio playback. Check its sound permission.",
      );
    }
  };
  return {
    state,
    error,
    captions,
    transcript,
    transcriptTruncated,
    audioActivity,
    visualizerError,
    muted,
    needsPlayback,
    start,
    end,
    toggleMute,
    resumePlayback,
    active:
      state === "connecting" || state === "listening" || state === "working",
  };
}

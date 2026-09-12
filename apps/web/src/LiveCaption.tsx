import { AssistantIdentity } from "./AssistantIdentity";
import { useEffect, useRef, useState } from "react";
import type { AudioActivity } from "./liveAudio";
import type { TranscriptTurn } from "./liveTranscript";

/** Stable, short pages of original text; never rewrites a spoken fact. */
export function captionPage(text: string) {
  let start = 0;
  // Prefer a fresh sentence so a short final phrase keeps its qualifiers.
  for (const boundary of text.matchAll(/(?<=[.!?])\s+(?=\S)/gu))
    start = boundary.index + boundary[0].length;
  const sentenceStart = start;
  const words = [...text.slice(sentenceStart).matchAll(/\S+\s*/gu)];
  let length = 0;
  for (const word of words) {
    if (length && length + word[0].length > 110) {
      start = sentenceStart + word.index;
      length = 0;
    }
    length += word[0].length;
  }
  return { text: text.slice(start).trim(), offset: start };
}

export function LiveCaption({
  turns,
  activity,
  needsPlayback,
  meterUnavailable,
  connecting,
  working,
  muted,
}: {
  turns: TranscriptTurn[];
  activity: AudioActivity;
  needsPlayback: boolean;
  meterUnavailable: boolean;
  connecting: boolean;
  working: boolean;
  muted: boolean;
}) {
  const [shown, setShown] = useState<TranscriptTurn | null>(null);
  const lastAudio = useRef(-Infinity);
  const interrupted = useRef<string | null>(null);
  const latest = turns.at(-1);
  const inputActive = !muted && activity.inputLevel > 0.025;
  const outputActive = !needsPlayback && activity.outputLevel > 0.025;

  useEffect(() => {
    if (connecting) {
      lastAudio.current = -Infinity;
      interrupted.current = null;
      setShown(null);
      return;
    }
    if (outputActive) lastAudio.current = Date.now();
    if (inputActive && !outputActive && latest?.speaker === "output") {
      interrupted.current = latest.id;
      setShown(null);
      return;
    }
    if (!latest) return;
    if (latest.speaker === "input") {
      setShown(latest);
      return;
    }
    if (latest.id === interrupted.current) return;
    // Transcript times are not playback times. Gate the visual handoff on
    // measured, unmuted audio, retaining a short allowance for late packets.
    // Captions remain usable when playback or audio analysis is unavailable.
    if (
      needsPlayback ||
      meterUnavailable ||
      Date.now() - lastAudio.current < 650
    )
      setShown(latest);
  }, [
    latest,
    inputActive,
    outputActive,
    needsPlayback,
    meterUnavailable,
    connecting,
  ]);

  const page = captionPage(shown?.text ?? "");
  const empty = connecting
    ? "A moment to connect."
    : muted
      ? "Your microphone is off."
      : working && !inputActive
        ? "Finding your answer…"
        : "Go ahead. I’m listening.";
  return (
    <div className="live-caption" data-speaker={shown?.speaker ?? "waiting"}>
      <div className="live-caption-label">
        <span className="live-caption-dot" aria-hidden="true" />
        {page.text ? (
          shown?.speaker === "output" ? (
            <AssistantIdentity compact />
          ) : (
            "You"
          )
        ) : (
          "Live"
        )}
        {needsPlayback && (
          <span className="live-caption-note">Sound paused</span>
        )}
      </div>
      <div className="live-caption-stage">
        <p
          key={`${shown?.id ?? "empty"}-${page.offset}`}
          className={
            page.text ? "live-caption-words" : "live-caption-placeholder"
          }
        >
          {page.text || empty}
        </p>
      </div>
    </div>
  );
}

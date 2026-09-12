import { useEffect, useRef, useState } from "react";
import { Icon } from "./Icon";
import { activeStatus, type Run } from "./api";
import type { useLive } from "./useLive";
import { VoiceGrid, type VoicePresenceState } from "./VoiceGrid";
import { TranscriptText } from "./TranscriptText";
import { LiveCaption } from "./LiveCaption";

export function VoiceBar({
  live,
  preparing,
  watching,
  run,
  summaryReady = false,
  onRetry,
  onRefresh,
  onUseQuestion,
  onDismiss,
}: {
  live: ReturnType<typeof useLive>;
  preparing: boolean;
  watching: boolean;
  run?: Run;
  summaryReady?: boolean;
  onRetry: () => void;
  onRefresh: () => void;
  onUseQuestion: () => void;
  onDismiss: () => void;
}) {
  const active = live.active || preparing;
  const [expanded, setExpanded] = useState(true);
  const [transcriptView, setTranscriptView] = useState(false);
  const [following, setFollowing] = useState(true);
  const transcript = useRef<HTMLDivElement>(null);
  const surface = useRef<HTMLElement>(null);
  const wasActive = useRef(active);
  const recappedRun = useRef<string | null>(null);
  const [speaker, setSpeaker] = useState<"input" | "output" | null>(null);
  const [elapsed, setElapsed] = useState(0);
  const answeredRunId = run?.answer ? run.id : null;
  useEffect(() => {
    if (!active) return;
    const started = Date.now();
    setElapsed(0);
    setExpanded(true);
    setTranscriptView(false);
    setFollowing(true);
    const timer = setInterval(
      () => setElapsed(Math.floor((Date.now() - started) / 1000)),
      1000,
    );
    return () => clearInterval(timer);
  }, [active]);
  useEffect(() => {
    if (active) {
      recappedRun.current = null;
    } else if (
      summaryReady &&
      answeredRunId &&
      recappedRun.current !== answeredRunId
    ) {
      recappedRun.current = answeredRunId;
      if (following && !transcript.current?.contains(document.activeElement))
        setExpanded(false);
    }
  }, [active, answeredRunId, following, summaryReady]);
  useEffect(() => {
    if (
      wasActive.current &&
      !active &&
      document.activeElement === document.body
    )
      surface.current?.focus({ preventScroll: true });
    wasActive.current = active;
  }, [active]);
  useEffect(() => {
    const next =
      live.audioActivity.outputLevel > 0.025
        ? "output"
        : live.audioActivity.inputLevel > 0.025
          ? "input"
          : null;
    if (!active) {
      setSpeaker(null);
      return;
    }
    if (next) {
      setSpeaker(next);
      return;
    }
    const timer = setTimeout(() => setSpeaker(null), 600);
    return () => clearTimeout(timer);
  }, [active, live.audioActivity.inputLevel, live.audioActivity.outputLevel]);
  useEffect(() => {
    if ((!active || transcriptView) && following && transcript.current)
      transcript.current.scrollTop = transcript.current.scrollHeight;
  }, [live.transcript, following, expanded, active, transcriptView]);
  useEffect(() => {
    const element = transcript.current;
    if (
      !element ||
      (active && !transcriptView) ||
      !following ||
      typeof ResizeObserver === "undefined"
    )
      return;
    const observer = new ResizeObserver(() => {
      element.scrollTop = element.scrollHeight;
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, [following, active, transcriptView]);
  const title =
    preparing || live.state === "connecting"
      ? "Connecting"
      : live.state === "error"
        ? "Voice unavailable"
        : !active
          ? "Voice ended"
          : speaker === "output" && !live.needsPlayback
            ? "Speaking"
            : live.muted
              ? "Muted"
              : speaker === "input"
                ? "Listening"
                : live.state === "working"
                  ? "Working on your answer"
                  : "Listening";
  const hasTranscript = live.transcript.length > 0;
  const presence: VoicePresenceState =
    preparing || live.state === "connecting"
      ? "connecting"
      : live.state === "error"
        ? "error"
        : !active
          ? "ended"
          : live.needsPlayback
            ? "sound-paused"
            : speaker === "output"
              ? "speaking"
              : live.muted
                ? "muted"
                : speaker === "input"
                  ? "listening"
                  : live.state === "working"
                    ? "working"
                    : "listening";
  const hint =
    presence === "connecting"
      ? "Getting your microphone and conversation ready."
      : presence === "working"
        ? "Checking the knowledge behind your question."
        : presence === "speaking"
          ? live.muted
            ? "Your microphone is muted. You can keep listening."
            : "You can interrupt or ask a follow-up."
          : presence === "muted"
            ? "Unmute whenever you’re ready."
            : presence === "sound-paused"
              ? "Enable sound below, or follow the transcript."
              : "Speak naturally. There’s no need to hold a button.";
  const detail =
    active || live.error || summaryReady
      ? ""
      : run
        ? activeStatus(run.status)
          ? "Finishing your answer in this chat."
          : run.answer
            ? "Answer saved in this chat."
            : "Your question and its status are in the chat."
        : hasTranscript
          ? watching
            ? "Checking for your last answer."
            : "No answer was saved."
          : "";
  return (
    <section
      ref={surface}
      id="voice-controls"
      tabIndex={-1}
      className={`voice-session ${active ? "is-live" : "is-ended"} ${expanded ? "is-expanded" : ""}`}
      aria-label="Live conversation"
      data-state={presence}
    >
      <div className="voice-presence">
        {active && <VoiceGrid activity={live.audioActivity} state={presence} />}
        <div className="voice-presence-copy">
          <h2 role="status" aria-atomic="true">
            {presence === "sound-paused" ? "Sound is paused" : title}
          </h2>
          {active && <p className="voice-state-hint">{hint}</p>}
          {(active || elapsed > 0) && (
            <span className="voice-duration" aria-label={`${elapsed} seconds`}>
              {Math.floor(elapsed / 60)}:{String(elapsed % 60).padStart(2, "0")}
            </span>
          )}
        </div>
        <div className="voice-session-actions">
          {hasTranscript && !active && (
            <button
              className="voice-icon-control"
              aria-label={expanded ? "Hide transcript" : "Show transcript"}
              title={expanded ? "Hide transcript" : "Show transcript"}
              aria-expanded={expanded}
              aria-controls="voice-session-body"
              onClick={() => setExpanded((value) => !value)}
            >
              <Icon name="chat" size={16} />
            </button>
          )}
          {!live.active && !preparing && live.error && (
            <button className="voice-retry" onClick={onRetry}>
              Try again
            </button>
          )}
          {!active && (
            <button
              className="voice-icon-control"
              aria-label="Dismiss voice recap"
              onClick={onDismiss}
            >
              <Icon name="close" size={16} />
            </button>
          )}
        </div>
      </div>
      {active && (
        <div className="voice-view-switch" role="group" aria-label="Voice view">
          <button
            aria-pressed={!transcriptView}
            onClick={() => setTranscriptView(false)}
          >
            <Icon name="audio" size={14} /> Focus
          </button>
          <button
            aria-pressed={transcriptView}
            onClick={() => {
              setTranscriptView(true);
              setFollowing(true);
            }}
          >
            <Icon name="chat" size={14} /> Transcript
          </button>
        </div>
      )}
      {live.needsPlayback && (
        <button
          className="voice-enable"
          onClick={() => void live.resumePlayback()}
        >
          <Icon name="headphones" size={17} /> Enable sound
        </button>
      )}
      {live.error && (
        <p className="voice-error" role="alert">
          {live.error}
        </p>
      )}
      {detail && (
        <p className="voice-outcome" role="status">
          {detail}
        </p>
      )}
      <div
        className={`voice-expand-region ${active || (expanded && hasTranscript) ? "is-open" : ""}`}
        aria-hidden={!active && (!expanded || !hasTranscript)}
        inert={!active && (!expanded || !hasTranscript)}
      >
        <div id="voice-session-body" className="voice-expand-content">
          {!active && live.transcriptTruncated && (
            <p className="voice-visualizer-notice">
              Showing the most recent live text. Earlier captions have left this
              view.
            </p>
          )}
          <div
            className={
              active && !transcriptView
                ? "voice-caption-surface"
                : "voice-transcript-scroll"
            }
            ref={transcript}
            role="region"
            aria-label="Live transcript"
            tabIndex={0}
            onScroll={(event) => {
              if (active && !transcriptView) return;
              const element = event.currentTarget;
              setFollowing(
                element.scrollHeight -
                  element.scrollTop -
                  element.clientHeight <
                  35,
              );
            }}
          >
            {active && !transcriptView ? (
              <LiveCaption
                turns={live.transcript}
                activity={live.audioActivity}
                needsPlayback={live.needsPlayback}
                meterUnavailable={!!live.visualizerError}
                connecting={preparing || live.state === "connecting"}
                working={live.state === "working"}
                muted={live.muted}
              />
            ) : (
              <TranscriptText turns={live.transcript} />
            )}
          </div>
          {(!active || transcriptView) && !following && (
            <button
              className="transcript-follow"
              onClick={() => setFollowing(true)}
            >
              <Icon name="arrow" size={13} /> Jump to latest
            </button>
          )}
        </div>
      </div>
      {active && transcriptView && (
        <p className="voice-transcript-note">
          Transcript text can arrive before the spoken audio.
        </p>
      )}
      {live.visualizerError && (
        <p className="voice-visualizer-notice">{live.visualizerError}</p>
      )}
      {!active && !run && hasTranscript && !summaryReady && (
        <div className="voice-recovery">
          <button className="text-button" onClick={onRefresh}>
            Check for answer
          </button>
          {live.captions.input && (
            <button className="text-button" onClick={onUseQuestion}>
              Continue in chat <Icon name="arrow" size={13} />
            </button>
          )}
        </div>
      )}
    </section>
  );
}

import { useId, useRef, useState, type ReactNode } from "react";
import type { Conversation, Run } from "./api";
import { ConversationRecap } from "./ConversationRecap";
import "./recap-review.css";

export function ConversationReview({
  conversation,
  runs,
  transcript,
  voiceError,
  watching = false,
  collapsed = false,
  onRefresh,
}: {
  conversation: Conversation;
  runs?: readonly Run[];
  transcript?: ReactNode;
  voiceError?: string;
  watching?: boolean;
  collapsed?: boolean;
  onRefresh: () => void;
}) {
  const [tab, setTab] = useState<"recap" | "transcript">("recap");
  const id = useId();
  const tabs = useRef<HTMLDivElement>(null);
  const review = (
    <section className="conversation-review" aria-label="Conversation review">
      {voiceError && (
        <p className="review-error" role="alert">
          {voiceError}
        </p>
      )}
      {transcript && (
        <div
          ref={tabs}
          className="review-tabs"
          role="tablist"
          aria-label="Conversation view"
          onKeyDown={(event) => {
            if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key))
              return;
            event.preventDefault();
            const next =
              event.key === "Home"
                ? "recap"
                : event.key === "End"
                  ? "transcript"
                  : tab === "recap"
                    ? "transcript"
                    : "recap";
            setTab(next);
            tabs.current
              ?.querySelector<HTMLButtonElement>(`[data-tab="${next}"]`)
              ?.focus();
          }}
        >
          {(["recap", "transcript"] as const).map((value) => (
            <button
              key={value}
              role="tab"
              data-tab={value}
              id={`${id}-${value}-tab`}
              aria-controls={`${id}-${value}`}
              aria-selected={tab === value}
              tabIndex={tab === value ? 0 : -1}
              onClick={() => setTab(value)}
            >
              {value === "recap" ? "Recap" : "Transcript"}
            </button>
          ))}
        </div>
      )}
      <div
        id={`${id}-recap`}
        role={transcript ? "tabpanel" : undefined}
        tabIndex={transcript ? 0 : undefined}
        aria-labelledby={transcript ? `${id}-recap-tab` : undefined}
        hidden={!!transcript && tab !== "recap"}
      >
        <ConversationRecap
          conversation={conversation}
          runs={collapsed ? [] : runs}
          onRefresh={onRefresh}
          embedded
        />
        {transcript &&
          conversation.summary_status === "idle" &&
          !conversation.transcripts?.length &&
          (watching ? (
            <p className="review-pending" role="status">
              Checking for your saved conversation…
            </p>
          ) : (
            <div className="review-pending" role="alert">
              <p>
                The saved transcript is not available yet. Your live text is
                still in Transcript.
              </p>
              <button className="text-button" onClick={onRefresh}>
                Check again
              </button>
            </div>
          ))}
      </div>
      {transcript && (
        <div
          id={`${id}-transcript`}
          role="tabpanel"
          tabIndex={0}
          aria-labelledby={`${id}-transcript-tab`}
          hidden={tab !== "transcript"}
        >
          {transcript}
        </div>
      )}
    </section>
  );
  if (!collapsed) return review;
  return (
    <details className="typed-conversation-recap">
      <summary>
        Conversation recap
        {conversation.summary_status === "failed" ? " · unavailable" : ""}
      </summary>
      {review}
    </details>
  );
}

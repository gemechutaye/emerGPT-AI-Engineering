import { useState } from "react";
import { useInfiniteQuery } from "@tanstack/react-query";
import {
  api,
  type Conversation,
  type SavedTranscript,
  type TranscriptPage,
} from "./api";
import { transcriptTurns } from "./liveTranscript";
import { Icon } from "./Icon";
import { TranscriptText } from "./TranscriptText";

export function TranscriptArchive({
  owner,
  conversation,
  embedded = false,
  watching = false,
}: {
  owner: string;
  conversation: Conversation;
  embedded?: boolean;
  watching?: boolean;
}) {
  const [expanded, setExpanded] = useState(!conversation.summary);
  const pages = useInfiniteQuery({
    queryKey: [owner, "voice-transcripts", conversation.id],
    initialPageParam: null as number | null,
    queryFn: ({ pageParam }) =>
      api<TranscriptPage>(
        `/conversations/${conversation.id}/transcripts${pageParam ? `?after=${pageParam}` : ""}`,
      ),
    getNextPageParam: (page) => page.next_cursor ?? undefined,
    initialData: {
      pages: [
        {
          items: conversation.transcripts ?? [],
          next_cursor: conversation.transcripts_next_cursor ?? null,
        },
      ],
      pageParams: [null],
    },
    enabled: embedded || expanded,
    refetchInterval: watching ? 1400 : false,
  });
  const fragments = [
    ...new Map(
      pages.data.pages
        .flatMap((page) => page.items)
        .map((fragment) => [fragment.event_id, fragment]),
    ).values(),
  ];
  const sessions = new Map<string, SavedTranscript[]>();
  fragments.forEach((fragment) => {
    const group = sessions.get(fragment.live_session_id) ?? [];
    group.push(fragment);
    sessions.set(fragment.live_session_id, group);
  });
  const body = (
    <div className="saved-transcript-body">
      {[...sessions].map(([id, group]) => (
        <section key={id} aria-label="Saved voice session">
          <h3>
            <time dateTime={group[0].created_at}>
              {new Date(group[0].created_at).toLocaleString(undefined, {
                month: "short",
                day: "numeric",
                hour: "numeric",
                minute: "2-digit",
              })}
            </time>
          </h3>
          <TranscriptText
            turns={transcriptTurns(
              group.map((fragment, order) => ({
                id: fragment.event_id,
                speaker: fragment.speaker === "user" ? "input" : "output",
                text: fragment.delta,
                start: fragment.start_ms,
                end: fragment.end_ms,
                order,
              })),
            )}
          />
        </section>
      ))}
      {pages.error && (
        <div role="alert" className="recap-error">
          <p>{pages.error.message}</p>
          <button
            className="text-button"
            onClick={() =>
              void (pages.isFetchNextPageError
                ? pages.fetchNextPage()
                : pages.refetch())
            }
          >
            Retry transcript
          </button>
        </div>
      )}
      {pages.hasNextPage && (
        <button
          className="text-button"
          disabled={pages.isFetching}
          onClick={() => void pages.fetchNextPage()}
        >
          {pages.isFetchingNextPage ? "Loading…" : "Load more transcript"}
        </button>
      )}
    </div>
  );
  return embedded ? (
    body
  ) : (
    <details
      className="transcript-archive"
      open={expanded}
      onToggle={(event) => setExpanded(event.currentTarget.open)}
    >
      <summary>
        <Icon name="chat" size={16} />
        <span>Voice transcript</span>
        <Icon name="chevron" size={13} />
      </summary>
      {body}
    </details>
  );
}

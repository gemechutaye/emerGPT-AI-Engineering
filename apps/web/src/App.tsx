import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type FormEvent,
  type ReactNode,
} from "react";
import {
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
  type InfiniteData,
} from "@tanstack/react-query";
import {
  activeStatus,
  api,
  idempotencyKey,
  post,
  type Answer,
  type Conversation,
  type Run,
} from "./api";
import { Icon } from "./Icon";
import { useLive } from "./useLive";
import { VoiceBar } from "./VoiceBar";
import { RunActivity } from "./RunActivity";
import { AnswerSources } from "./AnswerSources";
import { Sidebar } from "./Sidebar";
import { bootstrapSession, useHistorySync } from "./historySync";
import { ConversationReview } from "./ConversationReview";
import { TranscriptArchive } from "./TranscriptArchive";
import { TranscriptText } from "./TranscriptText";
import { Brand } from "./Brand";
import { AssistantIdentity } from "./AssistantIdentity";
import { AppearanceMenu } from "./AppearanceMenu";
import { HomeDisco } from "./HomeDisco";
import { SignInPlaceholder } from "./SignInPlaceholder";
import { InsideEmer, InsideMark } from "./InsideEmer";
import { syncConversationLists } from "./conversationCache";

const examples = [
  {
    label: "Patient overview",
    question:
      "What do we know about PT-006, and what information is still missing before they decide?",
    icon: "patient" as const,
  },
  {
    label: "Policy changes",
    question:
      "How did the cancellation policy differ in March and September 2026?",
    icon: "clock" as const,
  },
  {
    label: "Follow-ups",
    question:
      "Which patients have follow-up intervals recorded, and can we tell if anyone is overdue?",
    icon: "chat" as const,
  },
];
const readableError = (error: unknown) =>
  error instanceof Error
    ? error.message
    : "The request could not be completed.";
const gapText = (
  value: string | { text?: string; question?: string; reason?: string },
) =>
  typeof value === "string"
    ? value
    : (value.text ?? value.reason ?? value.question ?? "");

function ErrorNotice({
  error,
  title = "Something needs attention",
  action,
}: {
  error: unknown;
  title?: string;
  action?: ReactNode;
}) {
  return (
    <div className="error-notice" role="alert">
      <div>
        <strong>{title}</strong>
        <p>{readableError(error)}</p>
      </div>
      {action}
    </div>
  );
}

function AnswerBody({ answer }: { answer: Answer }) {
  const scopes = (answer.scopes ?? []).filter(
    (scope, index, all) =>
      all.findIndex(
        (other) =>
          other.as_of === scope.as_of &&
          other.all_patients === scope.all_patients &&
          other.patient_discovery === scope.patient_discovery &&
          JSON.stringify(other.patient_ids) ===
            JSON.stringify(scope.patient_ids) &&
          JSON.stringify(other.unknown_patient_ids) ===
            JSON.stringify(scope.unknown_patient_ids),
      ) === index,
  );
  return (
    <>
      {!!answer.scopes?.length && (
        <div className="resolved-scopes" aria-label="Resolved answer context">
          {scopes.map((scope) => (
            <span key={scope.part_id} title={scope.question}>
              {scope.all_patients
                ? "All patient records searched"
                : scope.patient_discovery
                  ? "Patient records searched"
                  : scope.patient_ids.length
                    ? scope.patient_ids.join(", ")
                    : scope.unknown_patient_ids.length
                      ? scope.unknown_patient_ids.join(", ")
                      : "Practice knowledge"}
              <i />
              Knowledge as of {scope.as_of}
            </span>
          ))}
        </div>
      )}
      <div className="answer-prose">
        {answer.statements?.map((statement, i) => (
          <p key={i}>{statement.text}</p>
        ))}
      </div>
      {!!answer.gaps?.length && (
        <div
          className={`knowledge-gap ${answer.status === "clarification" ? "clarification" : ""}`}
        >
          <div className="gap-marker">
            {answer.status === "clarification" ? "?" : "!"}
          </div>
          <div>
            <h4>
              {answer.status === "clarification"
                ? "A quick clarification"
                : answer.status === "unsupported"
                  ? "Not enough information"
                  : "Still unknown"}
            </h4>
            {answer.gaps.map((gap, i) => (
              <p key={i}>{gapText(gap)}</p>
            ))}
          </div>
        </div>
      )}
      {!!answer.next_steps?.length && (
        <div className="next-steps">
          <h4>Next steps</h4>
          {answer.next_steps.map((step, i) => (
            <p key={i}>
              <span className="step-marker">{i + 1}</span>
              <span>{typeof step === "string" ? step : step.text}</span>
            </p>
          ))}
        </div>
      )}
      {!answer.statements?.length && !answer.gaps?.length && (
        <p role="status">
          No answer was returned. Your question is saved; try asking again.
        </p>
      )}
      <AnswerSources answer={answer} />
    </>
  );
}

function RunView({
  run,
  owner,
  onRefresh,
}: {
  run: Run;
  owner?: string;
  onRefresh: () => void;
}) {
  const cancel = useMutation({
    mutationFn: () => post(`/runs/${run.id}/cancel`),
    onSuccess: onRefresh,
  });
  const active = activeStatus(run.status);
  // Only an answer that completes while this turn is mounted gets an entrance.
  // Saved answers open immediately; no simulated token stream or hidden prose.
  const [animateAnswer] = useState(active);
  const failed =
    !active && run.status !== "completed" && run.status !== "cancelled";
  return (
    <article className="turn">
      <div className="question-row">
        <span className="sr-only">You</span>
        <h2>{run.question}</h2>
      </div>
      <div className="answer-row">
        <div className="answer-content">
          <div className="answer-heading">
            <AssistantIdentity active={active} />
            <span className="answer-label">
              {active
                ? ""
                : failed
                  ? "Couldn't complete"
                  : run.status === "cancelled"
                    ? "Stopped"
                    : run.answer?.status === "clarification"
                      ? "Quick question"
                      : run.answer?.status === "partial"
                        ? "Some details missing"
                        : run.answer?.status === "unsupported"
                          ? "Not in these records"
                          : run.answer
                            ? ""
                            : "No answer returned"}
            </span>
          </div>
          <div className="answer-activity">
            <RunActivity run={run} owner={owner} onRefresh={onRefresh} />
            {active && (
              <button
                className="text-button"
                disabled={cancel.isPending || run.status === "cancelling"}
                onClick={() => cancel.mutate()}
              >
                Stop
              </button>
            )}
          </div>
          {run.answer && (
            <div
              className={
                animateAnswer
                  ? "published-answer answer-reveal"
                  : "published-answer"
              }
            >
              <AnswerBody answer={run.answer} />
            </div>
          )}
          {!active && run.status === "cancelled" && (
            <p className="cancelled-notice" role="status">
              This answer was stopped. Your question is saved above.
            </p>
          )}
          {(failed ||
            (!active && !run.answer && run.status !== "cancelled")) && (
            <ErrorNotice
              title="Answer could not be completed"
              error={
                new Error(
                  typeof run.error === "string"
                    ? run.error
                    : (run.error?.message ??
                        "Your question is saved. Try submitting it again."),
                )
              }
            />
          )}
          {cancel.error && (
            <ErrorNotice
              error={cancel.error}
              title="Could not stop this answer"
            />
          )}
        </div>
      </div>
    </article>
  );
}

export default function App() {
  const queryClient = useQueryClient();
  const [sessionExpired, setSessionExpired] = useState(false);
  const expiredRef = useRef(false);
  const navigationEpoch = useRef(0);
  const [selection, setSelection] = useState<{
    owner?: string;
    id: string | null;
  }>({ id: null });
  const [question, setQuestion] = useState("");
  const [railOpen, setRailOpen] = useState(false);
  const [inside, setInside] = useState(
    window.location.hash.startsWith("#inside"),
  );
  const navigateInside = (show: boolean) => {
    window.history.pushState(
      null,
      "",
      `${window.location.pathname}${window.location.search}${show ? "#inside" : ""}`,
    );
    setInside(show);
  };
  useEffect(() => {
    const sync = () => setInside(window.location.hash.startsWith("#inside"));
    window.addEventListener("hashchange", sync);
    window.addEventListener("popstate", sync);
    return () => {
      window.removeEventListener("hashchange", sync);
      window.removeEventListener("popstate", sync);
    };
  }, []);
  const [brandRestored, setBrandRestored] = useState(false);
  const [followingAnswers, setFollowingAnswers] = useState(true);
  const conversationViewport = useRef<HTMLElement>(null);
  const navigationTrigger = useRef<HTMLButtonElement>(null);
  const [actionError, setActionError] = useState<unknown>(null);
  const [liveRunId, setLiveRunId] = useState<string | null>(null);
  const [preparingLive, setPreparingLive] = useState(false);
  const [liveWatching, setLiveWatching] = useState(false);
  const [voiceVisible, setVoiceVisible] = useState(false);
  const voiceBaseline = useRef(new Set<string>());
  const preparingLiveRef = useRef(false);
  const creatingConversation = useRef<Promise<Conversation> | null>(null);
  const input = useRef<HTMLTextAreaElement>(null);
  const focusComposer = () => {
    const epoch = navigationEpoch.current;
    requestAnimationFrame(() => {
      if (
        epoch === navigationEpoch.current &&
        !document
          .querySelector(".chat-sidebar")
          ?.contains(document.activeElement) &&
        !document.querySelector("dialog[open]") &&
        !navigationTrigger.current?.contains(document.activeElement)
      )
        input.current?.focus();
    });
  };
  const submitIntent = useRef<{ fingerprint: string; key: string } | null>(
    null,
  );
  const session = useQuery({
    queryKey: ["session"],
    queryFn: bootstrapSession,
    staleTime: Infinity,
    retry: false,
    enabled: !sessionExpired,
  });
  const owner = sessionExpired ? undefined : session.data?.id;
  useHistorySync(queryClient, owner);
  const conversationId = selection.owner === owner ? selection.id : null;
  const history = useInfiniteQuery({
    queryKey: [owner, "conversation", conversationId],
    queryFn: ({ pageParam }) =>
      api<Conversation>(
        `/conversations/${conversationId}${pageParam ? `?before=${encodeURIComponent(pageParam)}` : ""}`,
      ),
    initialPageParam: null as string | null,
    getNextPageParam: (page) => page.next_cursor ?? undefined,
    enabled: !!owner && !!conversationId,
    refetchInterval: (query) =>
      liveWatching ||
      ["pending", "generating"].includes(
        query.state.data?.pages[0]?.summary_status ?? "",
      ) ||
      query.state.data?.pages[0]?.runs?.some((run) => activeStatus(run.status))
        ? 1400
        : false,
  });
  const current = history.data?.pages[0];
  useEffect(() => {
    if (owner && current) syncConversationLists(queryClient, owner, current);
  }, [queryClient, owner, current]);
  useEffect(() => {
    document.title = inside
      ? "The EMER dataset — emer-GPT"
      : current?.title
        ? `${current.title} — emer-GPT`
        : "emer-GPT";
  }, [current?.title, inside]);
  const refresh = useCallback(() => {
    void queryClient.invalidateQueries({
      queryKey: [owner, "conversation", conversationId],
    });
    void queryClient.invalidateQueries({ queryKey: [owner, "conversations"] });
    void queryClient.invalidateQueries({
      queryKey: [owner, "voice-transcripts", conversationId],
    });
  }, [queryClient, owner, conversationId]);
  const live = useLive((runId) => {
    setLiveRunId(runId);
    refresh();
  });
  useEffect(() => {
    if (live.state !== "ended" && live.state !== "error") return;
    refresh();
    // A completed earlier turn does not prove the final delegation has persisted.
    const timer = setTimeout(() => setLiveWatching(false), 90000);
    return () => clearTimeout(timer);
  }, [live.state, refresh]);
  useEffect(() => {
    const expire = () => {
      if (expiredRef.current) return;
      expiredRef.current = true;
      navigationEpoch.current += 1;
      creatingConversation.current = null;
      submitIntent.current = null;
      setSessionExpired(true);
      void live.end();
      setSelection({ id: null });
      setLiveRunId(null);
      setLiveWatching(false);
      setVoiceVisible(false);
      setActionError(null);
      queryClient.clear();
    };
    const otherTabReset = (event: StorageEvent) => {
      if (event.key === "emer:session-reset") expire();
    };
    window.addEventListener("emer:session-expired", expire);
    window.addEventListener("storage", otherTabReset);
    return () => {
      window.removeEventListener("emer:session-expired", expire);
      window.removeEventListener("storage", otherTabReset);
    };
  }, [queryClient, live.end]);
  const liveRun = useQuery({
    queryKey: [owner, "live-run", liveRunId],
    queryFn: () => api<Run>(`/runs/${liveRunId}`),
    enabled: !!owner && !!liveRunId,
    refetchInterval: (query) =>
      query.state.data && activeStatus(query.state.data.status) ? 1000 : false,
  });
  useEffect(() => {
    if (liveRun.data && !activeStatus(liveRun.data.status)) refresh();
  }, [liveRun.data?.status, refresh]);
  useEffect(() => {
    if (owner)
      setSelection({
        owner,
        id: window.localStorage.getItem(`emer:conversation:${owner}`),
      });
  }, [owner]);
  useEffect(() => {
    if (!owner || selection.owner !== owner) return;
    if (selection.id)
      window.localStorage.setItem(`emer:conversation:${owner}`, selection.id);
    else window.localStorage.removeItem(`emer:conversation:${owner}`);
  }, [owner, selection]);
  const closeNavigation = () => {
    setRailOpen(false);
    setBrandRestored(true);
    requestAnimationFrame(() =>
      navigationTrigger.current?.focus({ preventScroll: true }),
    );
  };

  const runs = [
    ...new Map(
      (
        history.data?.pages
          .slice()
          .reverse()
          .flatMap((page) => page.runs ?? []) ?? []
      ).map((run) => [run.id, run]),
    ).values(),
  ];
  const voiceRun = [...runs]
    .reverse()
    .find((run) => !voiceBaseline.current.has(run.id));
  const visibleRuns = runs.filter(
    (run) =>
      run.context_version === undefined ||
      run.context_version === current?.context_version,
  );
  const previousRuns = runs.filter(
    (run) =>
      run.context_version !== undefined &&
      run.context_version !== current?.context_version,
  );
  const running = runs.some((run) => activeStatus(run.status));
  const createConversation = async (title: string) => {
    if (!owner || expiredRef.current)
      throw new Error("Reconnect before starting a conversation.");
    if (conversationId) {
      if (current) return current;
      throw new Error(
        "Wait for this conversation to load, or retry loading it.",
      );
    }
    if (creatingConversation.current) return creatingConversation.current;
    const epoch = navigationEpoch.current;
    const pending = post<Conversation>("/conversations", { title })
      .then((value) => {
        if (epoch !== navigationEpoch.current || expiredRef.current)
          throw new Error(
            "The conversation changed before this request finished.",
          );
        setSelection({ owner, id: value.id });
        queryClient.setQueryData<InfiniteData<Conversation>>(
          [owner, "conversation", value.id],
          { pages: [value], pageParams: [null] },
        );
        void queryClient.invalidateQueries({
          queryKey: [owner, "conversations"],
        });
        return value;
      })
      .finally(() => {
        if (creatingConversation.current === pending)
          creatingConversation.current = null;
      });
    creatingConversation.current = pending;
    return pending;
  };
  const send = useMutation({
    mutationFn: async (text: string) => {
      const epoch = navigationEpoch.current;
      const conversation = await createConversation("New conversation");
      const fingerprint = `${conversation.id}:${conversation.context_version}:${text}`;
      if (submitIntent.current?.fingerprint !== fingerprint)
        submitIntent.current = { fingerprint, key: idempotencyKey() };
      const run = await post<Run>(`/conversations/${conversation.id}/runs`, {
        automatic_context: true,
        question: text,
        context_version: conversation.context_version,
        idempotency_key: submitIntent.current.key,
      });
      return { run, epoch };
    },
    onSuccess: ({ run, epoch }, submittedQuestion) => {
      if (epoch !== navigationEpoch.current || expiredRef.current) return;
      setFollowingAnswers(true);
      voiceBaseline.current.add(run.id);
      setQuestion((currentQuestion) =>
        currentQuestion.trim() === submittedQuestion ? "" : currentQuestion,
      );
      submitIntent.current = null;
      queryClient.setQueryData<InfiniteData<Conversation>>(
        [owner, "conversation", run.conversation_id],
        (previous) =>
          previous
            ? {
                ...previous,
                pages: previous.pages.map((page, index) =>
                  index === 0
                    ? {
                        ...page,
                        runs: [
                          ...(page.runs ?? []).filter(
                            (item) => item.id !== run.id,
                          ),
                          run,
                        ],
                      }
                    : page,
                ),
              }
            : previous,
      );
      void queryClient.invalidateQueries({
        queryKey: [owner, "conversation", run.conversation_id],
      });
      void queryClient.invalidateQueries({
        queryKey: [owner, "conversations"],
      });
      focusComposer();
    },
  });
  const switchConversation = async (id: string | null) => {
    if (inside) navigateInside(false);
    navigationEpoch.current += 1;
    creatingConversation.current = null;
    submitIntent.current = null;
    setLiveWatching(false);
    setVoiceVisible(false);
    setLiveRunId(null);
    setFollowingAnswers(true);
    setSelection({ owner, id });
    if (!id) setQuestion("");
    if (window.innerWidth < 1100) setRailOpen(false);
    setActionError(null);
    send.reset();
    await live.end();
    focusComposer();
  };
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (
      question.trim() &&
      !running &&
      !send.isPending &&
      !preparingLive &&
      !live.active &&
      (!conversationId || !!current) &&
      owner
    )
      send.mutate(question.trim());
  };
  const startLive = async () => {
    if (preparingLiveRef.current || !owner || expiredRef.current) return;
    const epoch = navigationEpoch.current;
    preparingLiveRef.current = true;
    setPreparingLive(true);
    setFollowingAnswers(true);
    setVoiceVisible(true);
    voiceBaseline.current = new Set(runs.map((run) => run.id));
    setLiveWatching(true);
    setActionError(null);
    try {
      const conversation = await createConversation("Voice conversation");
      if (epoch !== navigationEpoch.current || expiredRef.current) return;
      await live.start(conversation.id, conversation.context_version);
    } catch (error) {
      if (epoch === navigationEpoch.current && !expiredRef.current) {
        setActionError(error);
        setLiveWatching(false);
      }
    } finally {
      preparingLiveRef.current = false;
      setPreparingLive(false);
    }
  };
  const setExample = (value: string) => {
    setQuestion(value);
    input.current?.focus();
  };
  const reconnect = () => {
    expiredRef.current = false;
    setSessionExpired(false);
    setActionError(null);
    send.reset();
    void session.refetch();
  };
  const hasTurns = !!conversationId || visibleRuns.length > 0 || voiceVisible;
  const voiceActive = live.active || preparingLive;
  const hasVoiceHistory =
    !!current?.transcripts?.length ||
    (voiceVisible && live.transcript.length > 0);
  const showReview =
    !!current &&
    !voiceActive &&
    (hasVoiceHistory ||
      current.summary_status !== "idle" ||
      (!voiceVisible &&
        runs.some((run) => run.status === "completed" && run.answer)));
  const voiceSurface = (
    <VoiceBar
      live={live}
      preparing={preparingLive}
      watching={liveWatching}
      run={voiceRun}
      summaryReady={!!current?.summary}
      onRetry={() => void startLive()}
      onRefresh={refresh}
      onUseQuestion={() =>
        setExample(
          [...live.transcript]
            .reverse()
            .find((turn) => turn.speaker === "input")?.text ??
            live.captions.input,
        )
      }
      onDismiss={() => setVoiceVisible(false)}
    />
  );
  const latestAnswer = visibleRuns.at(-1);
  useLayoutEffect(() => {
    const viewport = conversationViewport.current;
    if (followingAnswers && viewport)
      viewport.scrollTop = viewport.scrollHeight;
  }, [latestAnswer, followingAnswers, voiceActive]);
  const displayError = actionError ?? (!sessionExpired ? send.error : null);
  useLayoutEffect(() => {
    const field = input.current;
    if (!field) return;
    field.style.height = "auto";
    field.style.height = `${Math.min(field.scrollHeight, 180)}px`;
  }, [question, hasTurns]);
  const composer = (
    <div className="composer-area">
      <div className="composer-inner">
        {displayError && <ErrorNotice error={displayError} />}
        <form
          className={`composer ${voiceActive ? "voice-composer" : ""}`}
          onSubmit={submit}
        >
          <label className="sr-only" htmlFor="question">
            Your question
          </label>
          <textarea
            ref={input}
            id="question"
            placeholder={
              voiceActive
                ? "Write a follow-up for later…"
                : hasTurns
                  ? "Ask a follow-up…"
                  : "Ask emer-GPT"
            }
            value={question}
            onChange={(event) => setQuestion(event.target.value)}
            onKeyDown={(event) => {
              if (
                event.key === "Enter" &&
                !event.shiftKey &&
                !event.nativeEvent.isComposing
              ) {
                event.preventDefault();
                if (!send.isPending) event.currentTarget.form?.requestSubmit();
              }
            }}
            maxLength={6000}
            rows={1}
          />
          <div className="composer-toolbar">
            <div className="composer-actions">
              {live.active && (
                <button
                  type="button"
                  className="composer-mute quiet-icon"
                  onClick={live.toggleMute}
                  aria-label={
                    live.muted ? "Unmute microphone" : "Mute microphone"
                  }
                  data-tooltip={
                    live.muted ? "Unmute microphone" : "Mute microphone"
                  }
                  aria-pressed={live.muted}
                  disabled={live.state === "connecting"}
                >
                  <Icon name={live.muted ? "mic-off" : "mic"} size={18} />
                </button>
              )}
              <button
                className={`live-button ${live.active ? "active" : ""}`}
                type="button"
                aria-label={live.active ? "End voice" : "Voice"}
                aria-pressed={live.active}
                data-tooltip={live.active ? "End voice" : "Start voice"}
                disabled={
                  !owner ||
                  (!!conversationId && !current) ||
                  (preparingLive && !live.active)
                }
                onClick={(event) => {
                  if (live.active) {
                    event.currentTarget.focus({ preventScroll: true });
                    void live.end();
                  } else void startLive();
                }}
              >
                <Icon name={live.active ? "stop" : "audio"} size={17} />
                <span>
                  {live.active
                    ? "End voice"
                    : preparingLive
                      ? "Connecting…"
                      : "Voice"}
                </span>
              </button>
              {!voiceActive && (
                <button
                  className="send-button"
                  type="submit"
                  disabled={
                    !question.trim() ||
                    !owner ||
                    send.isPending ||
                    running ||
                    preparingLive ||
                    (!!conversationId && !current)
                  }
                  aria-label={
                    send.isPending ? "Submitting question" : "Send question"
                  }
                  data-tooltip="Send message"
                >
                  {send.isPending ? (
                    <span className="button-spinner" />
                  ) : (
                    <Icon name="arrow" size={17} />
                  )}
                </button>
              )}
            </div>
          </div>
        </form>
        <div className="workspace-footnote">
          <span>Synthetic records. Not for clinical decisions.</span>
        </div>
      </div>
    </div>
  );
  return (
    <div className={`app-shell ${railOpen ? "history-open" : ""}`}>
      <a className="skip-link" href={inside ? "#inside-content" : "#question"}>
        {inside ? "Skip to content" : "Skip to question"}
      </a>
      <header className="app-topbar">
        <button
          ref={navigationTrigger}
          className={`brand brand-toggle ${brandRestored ? "is-restored" : ""}`}
          aria-label={railOpen ? "Close navigation" : "Open sidebar"}
          aria-expanded={railOpen}
          aria-controls="workspace-sidebar"
          data-tooltip={railOpen ? "Close sidebar" : "Open sidebar"}
          onPointerLeave={() => setBrandRestored(false)}
          onBlur={() => setBrandRestored(false)}
          onClick={() => {
            setBrandRestored(railOpen);
            setRailOpen((open) => !open);
          }}
        >
          <Brand navigation />
        </button>
        <div className="header-actions">
          {!hasTurns && !inside && <div id="home-disco-controls" />}
          <button
            className="inside-entry"
            aria-label="Dataset"
            aria-pressed={inside}
            onClick={() => navigateInside(!inside)}
            data-tooltip="Dataset"
          >
            <InsideMark />
            <span>Dataset</span>
          </button>
          <AppearanceMenu />
        </div>
      </header>
      {!railOpen && <SignInPlaceholder floating />}
      <Sidebar
        owner={owner}
        open={railOpen}
        currentId={conversationId}
        busy={preparingLive && !live.active}
        onClose={closeNavigation}
        onChoose={(id) => void switchConversation(id)}
        onNew={() => void switchConversation(null)}
        onChanged={refresh}
        onDeleted={(id) => {
          queryClient.removeQueries({ queryKey: [owner, "conversation", id] });
          if (id === conversationId) void switchConversation(null);
        }}
      />
      {inside && (
        <InsideEmer
          onBack={() => {
            navigateInside(false);
            focusComposer();
          }}
          voiceActive={voiceActive}
          onEndVoice={() => {
            if (preparingLive && !live.active) {
              navigationEpoch.current += 1;
              preparingLiveRef.current = false;
              setPreparingLive(false);
            }
            void live.end();
          }}
        />
      )}
      <div
        hidden={inside}
        inert={inside}
        aria-hidden={inside}
        className={`workspace ${hasTurns ? "has-turns" : "is-new"} ${voiceActive ? "is-voicing" : ""}`}
      >
        <div className="work-area">
          {!hasTurns && !inside && <HomeDisco />}
          <section className="task-heading">
            <h1>
              {hasTurns ? (
                current?.title ||
                (voiceVisible ? "Let's talk." : "Your conversation")
              ) : (
                <>
                  What would you
                  <br />
                  like to know?
                </>
              )}
            </h1>
            {hasTurns && (current?.patient_id || current?.as_of) && (
              <p className="inherited-context">
                Saved context:{" "}
                {[current.patient_id, current.as_of]
                  .filter(Boolean)
                  .join(" · ")}
              </p>
            )}
          </section>
          {sessionExpired && (
            <ErrorNotice
              title="This browser session has ended"
              error={
                new Error(
                  "Live has stopped. Reconnect to continue; your unsent text is still here.",
                )
              }
              action={
                <button className="button secondary" onClick={reconnect}>
                  Reconnect
                </button>
              }
            />
          )}
          {!sessionExpired && session.isPending && (
            <p className="connection-status" role="status">
              Connecting…
            </p>
          )}
          {!sessionExpired && session.error && (
            <ErrorNotice
              title="Could not connect to emer-GPT"
              error={session.error}
              action={
                <button
                  className="button secondary"
                  disabled={session.isFetching}
                  onClick={reconnect}
                >
                  {session.isFetching ? "Connecting…" : "Retry connection"}
                </button>
              }
            />
          )}
          {!hasTurns && composer}
          <div className="conversation-stage">
            <main
              ref={conversationViewport}
              id="conversation"
              className={`conversation ${visibleRuns.length || running ? "has-turns" : ""}`}
              onScroll={(event) => {
                const viewport = event.currentTarget;
                setFollowingAnswers(
                  viewport.scrollHeight -
                    viewport.scrollTop -
                    viewport.clientHeight <
                    48,
                );
              }}
            >
              {current && showReview && (
                <ConversationReview
                  key={current.id}
                  collapsed={!hasVoiceHistory}
                  conversation={current}
                  runs={runs}
                  onRefresh={refresh}
                  watching={liveWatching}
                  voiceError={voiceVisible ? live.error : undefined}
                  transcript={
                    hasVoiceHistory ? (
                      owner && current.transcripts?.length && !live.error ? (
                        <TranscriptArchive
                          owner={owner}
                          conversation={current}
                          embedded
                          watching={liveWatching}
                        />
                      ) : voiceVisible && live.transcript.length ? (
                        <div
                          className="saved-transcript-body"
                          role="region"
                          aria-label="Voice transcript"
                        >
                          {live.transcriptTruncated && (
                            <p className="review-pending">
                              Showing the latest live text. Reopen this chat for
                              the saved transcript.
                            </p>
                          )}
                          <TranscriptText turns={live.transcript} />
                        </div>
                      ) : owner ? (
                        <TranscriptArchive
                          owner={owner}
                          conversation={current}
                          embedded
                        />
                      ) : undefined
                    ) : undefined
                  }
                />
              )}
              {voiceVisible && !showReview && (
                <div
                  className={voiceActive ? "voice-stage" : "thread-voice-recap"}
                >
                  {voiceSurface}
                </div>
              )}
              <div className="conversation-content" hidden={voiceActive}>
                {conversationId && history.isPending && (
                  <p className="loading-line" role="status">
                    Loading conversation…
                  </p>
                )}
                {history.error && (
                  <ErrorNotice
                    title="Conversation could not load"
                    error={history.error}
                    action={
                      <button
                        className="button secondary"
                        disabled={history.isFetching}
                        onClick={() =>
                          void (history.isFetchNextPageError
                            ? history.fetchNextPage()
                            : history.refetch())
                        }
                      >
                        Reload conversation
                      </button>
                    }
                  />
                )}
                {liveRun.error && (
                  <ErrorNotice
                    title="Could not check your saved answer"
                    error={liveRun.error}
                    action={
                      <button
                        className="text-button"
                        onClick={() => void liveRun.refetch()}
                      >
                        Check again
                      </button>
                    }
                  />
                )}
                {!hasTurns && (
                  <section className="starter-work" aria-label="Ways to begin">
                    <div className="starter-list">
                      {examples.map((example) => (
                        <button
                          key={example.label}
                          onClick={() => setExample(example.question)}
                        >
                          <span className="starter-icon">
                            <Icon name={example.icon} size={18} />
                          </span>
                          <strong>{example.label}</strong>
                        </button>
                      ))}
                    </div>
                  </section>
                )}
                {current &&
                  !runs.length &&
                  !current.transcripts?.length &&
                  !current.summary &&
                  !voiceVisible &&
                  !history.error && (
                    <p className="history-start">
                      No messages in this chat yet.
                    </p>
                  )}
                {history.hasNextPage && (
                  <div className="earlier-answers">
                    <button
                      className="button secondary"
                      disabled={history.isFetching}
                      onClick={() => void history.fetchNextPage()}
                    >
                      {history.isFetchingNextPage
                        ? "Loading earlier answers…"
                        : "Load earlier answers"}
                    </button>
                    <span role="status">{runs.length} answers loaded</span>
                  </div>
                )}
                {!history.hasNextPage &&
                  (history.data?.pages.length ?? 0) > 1 && (
                    <p className="history-start" role="status">
                      Beginning of this conversation · {runs.length} answers
                      loaded
                    </p>
                  )}
                {!!previousRuns.length && (
                  <details className="previous-context">
                    <summary>
                      {previousRuns.length} earlier answer
                      {previousRuns.length === 1 ? "" : "s"} in a different
                      context
                    </summary>
                    <p>These answers keep their original patient/date scope.</p>
                    {previousRuns.map((run) => (
                      <RunView
                        key={run.id}
                        run={run}
                        owner={owner}
                        onRefresh={refresh}
                      />
                    ))}
                  </details>
                )}
                <div className="turns">
                  {visibleRuns.map((run) => (
                    <RunView
                      key={run.id}
                      run={run}
                      owner={owner}
                      onRefresh={refresh}
                    />
                  ))}
                </div>
              </div>
            </main>
            {!followingAnswers && hasTurns && !voiceActive && (
              <button
                className="conversation-follow"
                onClick={() => setFollowingAnswers(true)}
              >
                <Icon name="arrow" size={14} /> Latest
              </button>
            )}
          </div>
          {hasTurns && composer}
        </div>
      </div>
    </div>
  );
}

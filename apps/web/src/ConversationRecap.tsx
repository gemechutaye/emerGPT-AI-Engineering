import { AssistantIdentity } from "./AssistantIdentity";
import { useMutation } from "@tanstack/react-query";
import { post, type Conversation, type Run } from "./api";
import { AnswerSources } from "./AnswerSources";
import { Icon } from "./Icon";

export function RecapText({ text }: { text: string }) {
  const lines = text.split(/\n+/).filter((line) => line.trim());
  const bullets = lines.filter((line) => /^\s*[-•]\s/.test(line));
  return (
    <div className="recap-text">
      {lines
        .filter((line) => !/^\s*[-•]\s/.test(line))
        .map((line, index) => (
          <p key={index}>{line}</p>
        ))}
      {bullets.length > 0 && (
        <ul>
          {bullets.map((line, index) => (
            <li key={index}>{line.replace(/^\s*[-•]\s+/, "")}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

export function ConversationRecap({
  conversation,
  runs = conversation.runs ?? [],
  onRefresh,
  embedded = false,
}: {
  conversation: Conversation;
  runs?: readonly Run[];
  onRefresh: () => void;
  embedded?: boolean;
}) {
  const retry = useMutation({
    mutationFn: () => post(`/conversations/${conversation.id}/summary`),
    onSuccess: onRefresh,
  });
  const status = conversation.summary_status;
  const canCreate =
    status === "idle" &&
    (!!conversation.transcripts?.length ||
      (conversation.runs ?? runs).some(
        (run) => run.status === "completed" && run.answer,
      ));
  const citedRuns = [
    ...new Map(runs.map((run) => [run.id, run])).values(),
  ].filter(
    (run) =>
      run.conversation_id === conversation.id &&
      run.status === "completed" &&
      run.answer &&
      [...run.answer.statements, ...run.answer.next_steps].some(
        (statement) => statement.citations?.length,
      ),
  );
  if (
    !conversation.summary &&
    (!status || status === "idle") &&
    !canCreate &&
    !citedRuns.length
  )
    return null;
  return (
    <section
      className={`conversation-recap ${embedded ? "is-embedded" : ""}`}
      aria-label="Conversation recap"
    >
      <div className="recap-heading">
        {!embedded && <Icon name="chat" size={18} />}
        <h2 className={embedded ? "sr-only" : undefined}>Conversation recap</h2>
        {canCreate && !retry.error && (
          <button
            className="text-button recap-create"
            disabled={retry.isPending}
            onClick={() => retry.mutate()}
          >
            {retry.isPending ? "Requesting recap…" : "Create recap"}
          </button>
        )}
      </div>
      {conversation.summary && <RecapText text={conversation.summary} />}
      {["pending", "generating"].includes(status ?? "") && (
        <p className="recap-pending" role="status">
          <span className="activity-indicator" />
          Preparing your recap…
        </p>
      )}
      {(status === "failed" || retry.error) && (
        <div className="recap-error" role="alert">
          <p>
            {retry.error instanceof Error
              ? retry.error.message
              : conversation.summary_error ||
                "The recap couldn’t be created. Your conversation is still saved."}
          </p>
          <button
            className="text-button"
            disabled={retry.isPending}
            onClick={() => retry.mutate()}
          >
            {retry.isPending ? "Requesting recap…" : "Retry recap"}
          </button>
        </div>
      )}
      {conversation.summary && (
        <span className="recap-disclaimer">
          Conversation summary · not clinical advice
        </span>
      )}
      {citedRuns.length > 0 && (
        <section
          className="recap-sources"
          aria-label="Sources from checked answers"
        >
          <h3>Sources from checked answers</h3>
          <p className="recap-sources-note">
            Open a question to review the saved answer and its original sources.
          </p>
          {citedRuns.map((run) => (
            <details className="recap-answer" key={run.id}>
              <summary>
                <span>
                  <span className="recap-speaker">You asked</span>
                  <span className="recap-question">{run.question}</span>
                </span>
                <Icon name="chevron" size={16} />
              </summary>
              <div className="recap-answer-body">
                <h4 className="recap-speaker" aria-label="emer-GPT answered">
                  <AssistantIdentity compact />
                </h4>
                {run.answer!.statements.map((statement, index) => (
                  <p key={index}>{statement.text}</p>
                ))}
                {!!run.answer!.gaps.length && (
                  <div className="recap-answer-gaps">
                    <h4>Still unknown</h4>
                    {run.answer!.gaps.map((gap, index) => (
                      <p key={index}>{gap.text}</p>
                    ))}
                  </div>
                )}
                {!!run.answer!.next_steps.length && (
                  <div className="recap-answer-next">
                    <h4>Next steps</h4>
                    {run.answer!.next_steps.map((step, index) => (
                      <p key={index}>{step.text}</p>
                    ))}
                  </div>
                )}
                <AnswerSources answer={run.answer!} />
              </div>
            </details>
          ))}
        </section>
      )}
    </section>
  );
}

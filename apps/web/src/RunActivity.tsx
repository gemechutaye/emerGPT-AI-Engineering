import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { activeStatus, api, type Run } from "./api";
import { Icon } from "./Icon";
import { RetrievalDetails } from "./RetrievalDetails";

const operations: Record<string, string> = {
  query_embedding: "Embedding the question",
  generation: "Writing the answer",
  repair: "Refining the answer",
  support_check: "Checking the answer",
  text_intent_resolution: "Understanding your question",
  voice_intent_resolution: "Understanding your question",
};
const stages: Record<string, string> = {
  queued: "Waiting to start",
  retrieving: "Reading practice records",
  generating: "Preparing the answer",
  cancellation_requested: "Stopping",
};

function metricNumber(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) && value >= 0
    ? value
    : undefined;
}

function usageNumber(value: unknown, key: "total_tokens" | "cost") {
  if (!value || typeof value !== "object" || Array.isArray(value))
    return undefined;
  if (key === "cost")
    return "cost" in value ? metricNumber(value.cost) : undefined;
  const total =
    "total_tokens" in value ? metricNumber(value.total_tokens) : undefined;
  if (total !== undefined) return total;
  const input =
    "prompt_tokens" in value ? metricNumber(value.prompt_tokens) : undefined;
  const output =
    "completion_tokens" in value
      ? metricNumber(value.completion_tokens)
      : undefined;
  return input !== undefined && output !== undefined
    ? input + output
    : undefined;
}

function usageLabel(values: unknown[], key: "total_tokens" | "cost") {
  const numbers = values.map((value) => usageNumber(value, key));
  const known = numbers.filter((value): value is number => value !== undefined);
  if (!known.length) return "Not reported";
  const total = known.reduce((sum, value) => sum + value, 0);
  if (!Number.isFinite(total)) return "Not reported";
  const formatted =
    key === "cost"
      ? new Intl.NumberFormat("en-US", {
          style: "currency",
          currency: "USD",
          maximumFractionDigits: 8,
        }).format(total)
      : total.toLocaleString();
  return known.length < values.length ? `At least ${formatted}` : formatted;
}

export function RunActivity({
  run,
  owner,
  onRefresh,
}: {
  run: Run;
  owner?: string;
  onRefresh: () => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const [streamLost, setStreamLost] = useState(false);
  const [now, setNow] = useState(Date.now);
  const ongoing = activeStatus(run.status);
  const snapshot = useQuery({
    queryKey: [owner, "run-activity", run.id],
    queryFn: () => api<Run>(`/runs/${run.id}`),
    enabled: ongoing || expanded,
    retry: false,
    refetchInterval: (query) =>
      activeStatus(query.state.data?.status ?? run.status) ? 1000 : false,
  });
  const current = snapshot.data ?? run;
  const active = ongoing && activeStatus(current.status);
  const refetch = snapshot.refetch;
  useEffect(() => {
    if (!active) return;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [active]);
  useEffect(() => {
    if (ongoing && !active) onRefresh();
  }, [ongoing, active, onRefresh]);
  useEffect(() => {
    if (!ongoing && snapshot.data && activeStatus(snapshot.data.status))
      void refetch();
  }, [ongoing, snapshot.data?.status, refetch]);
  useEffect(() => {
    if (!active || typeof EventSource === "undefined") return;
    let disposed = false;
    const stream = new EventSource(`/api/v1/runs/${run.id}/events`);
    stream.onopen = () => {
      if (!disposed) setStreamLost(false);
    };
    stream.onerror = () => {
      if (!disposed) setStreamLost(true);
    };
    stream.addEventListener("run", () => {
      if (!disposed) void refetch();
    });
    return () => {
      disposed = true;
      stream.close();
    };
  }, [active, run.id, owner, refetch]);
  const attempts = current.provider_attempts ?? [];
  const dispatched = [...attempts]
    .reverse()
    .find((attempt) => attempt.status === "dispatched");
  const lastEvent = current.events?.at(-1);
  const label =
    run.status === "cancelling" || current.status === "cancelling"
      ? "Stopping"
      : dispatched
        ? (operations[dispatched.operation] ?? "Running a model request")
        : (stages[lastEvent?.type ?? ""] ??
          (run.status === "queued" ? "Waiting to start" : "Working"));
  const endedAt = current.completed_at ?? run.completed_at;
  const elapsed = Math.max(
    0,
    Math.floor(
      ((endedAt ? Date.parse(endedAt) : now) - Date.parse(current.created_at)) /
        1000,
    ),
  );
  const model = (dispatched ?? attempts.at(-1))?.model;
  const metrics = current.metrics;
  const processingMs = metricNumber(metrics.total_ms);
  const records = metricNumber(metrics.source_count);
  const recordedUsage = Array.isArray(metrics.usage)
    ? metrics.usage
    : undefined;
  const usage = recordedUsage?.length
    ? [
        ...recordedUsage,
        ...attempts.slice(recordedUsage.length).map(() => undefined),
      ]
    : attempts.map((attempt) => attempt.usage);
  const calls =
    current.provider_attempts || recordedUsage
      ? Math.max(attempts.length, recordedUsage?.length ?? 0)
      : undefined;
  const failureReasons =
    current.status === "failed" && Array.isArray(metrics.failure_detail)
      ? metrics.failure_detail.filter(
          (value): value is string =>
            typeof value === "string" && !!value.trim(),
        )
      : [];
  const hasMetrics =
    processingMs !== undefined ||
    records !== undefined ||
    calls !== undefined ||
    usage.length > 0;
  return (
    <details
      className={`run-activity ${active ? "is-active" : ""}`}
      onToggle={(event) => setExpanded(event.currentTarget.open)}
    >
      <summary>
        <span
          className={active && !snapshot.error ? "activity-stage" : ""}
          role={active ? "status" : undefined}
        >
          {snapshot.error
            ? "Answer details unavailable"
            : active
              ? label
              : "Answer details"}
        </span>
        {Number.isFinite(elapsed) && (active || endedAt) && (
          <span className="activity-time">{elapsed}s</span>
        )}
        <Icon name="chevron" size={12} />
      </summary>
      <div className="activity-detail">
        {snapshot.error && (
          <p role="alert">
            Answer details are unavailable. Your answer request is still saved.{" "}
            <button onClick={() => void refetch()}>Retry details</button>
          </p>
        )}
        {streamLost && active && !snapshot.error && (
          <p className="activity-note">
            Live feed reconnecting. Checking progress every second.
          </p>
        )}
        {snapshot.isPending && !snapshot.error && (
          <p role="status">Loading answer details…</p>
        )}
        {model && <p className="activity-model">{model}</p>}
        {hasMetrics && (
          <dl className="activity-metrics" aria-label="Recorded answer metrics">
            {processingMs !== undefined && (
              <div>
                <dt>Processing time</dt>
                <dd>
                  {(processingMs / 1000).toLocaleString(undefined, {
                    maximumFractionDigits: 2,
                  })}{" "}
                  s
                </dd>
              </div>
            )}
            {records !== undefined && (
              <div>
                <dt>Records read</dt>
                <dd>{records.toLocaleString()}</dd>
              </div>
            )}
            {calls !== undefined && (
              <div>
                <dt>Recorded calls</dt>
                <dd>{calls}</dd>
              </div>
            )}
            {usage.length > 0 && (
              <>
                <div>
                  <dt>Tokens</dt>
                  <dd>{usageLabel(usage, "total_tokens")}</dd>
                </div>
                <div>
                  <dt>Cost (USD)</dt>
                  <dd>{usageLabel(usage, "cost")}</dd>
                </div>
              </>
            )}
          </dl>
        )}
        <ol>
          {(current.events ?? [])
            .filter((event) =>
              ["queued", "retrieving"].includes(event.type ?? ""),
            )
            .map((event) => (
              <li key={`event-${event.sequence}`}>
                <span className="activity-step-dot" />
                <span>{stages[event.type ?? ""]}</span>
              </li>
            ))}
          {attempts.map((attempt) => (
            <li key={attempt.id}>
              <span className={`activity-step-dot ${attempt.status}`} />
              <span>
                {operations[attempt.operation] ?? attempt.operation}
                <small>
                  {attempt.status === "dispatched"
                    ? active
                      ? "In progress"
                      : "No completion recorded"
                    : attempt.status === "completed"
                      ? "Complete"
                      : attempt.status === "uncertain"
                        ? "Outcome unconfirmed"
                        : attempt.status === "failed"
                          ? "Failed"
                          : "Status unavailable"}
                </small>
                {metricNumber(attempt.usage?.latency_ms) !== undefined && (
                  <small>
                    {(
                      metricNumber(attempt.usage?.latency_ms)! / 1000
                    ).toLocaleString(undefined, {
                      maximumFractionDigits: 3,
                    })}{" "}
                    s provider latency
                  </small>
                )}
              </span>
            </li>
          ))}
        </ol>
        <RetrievalDetails run={current} />
        {failureReasons.length > 0 && (
          <div className="activity-failure">
            <h4>Why this answer stopped</h4>
            <ul>
              {failureReasons.map((reason, index) => (
                <li key={index}>{reason}</li>
              ))}
            </ul>
          </div>
        )}
        {!snapshot.isPending &&
          !snapshot.error &&
          !attempts.length &&
          !current.events?.length &&
          !hasMetrics &&
          !current.answer?.diagnostics?.retrieval &&
          !failureReasons.length && (
            <p>No activity details were recorded for this answer.</p>
          )}
      </div>
    </details>
  );
}

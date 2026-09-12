import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { RunActivity } from "../RunActivity";
import type { Run } from "../api";

class EventStream extends EventTarget {
  static instances: EventStream[] = [];
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  close = vi.fn();
  constructor(readonly url: string) {
    super();
    EventStream.instances.push(this);
  }
}
const run: Run = {
  id: "run-activity-fixture",
  conversation_id: "chat",
  context_version: 1,
  index_id: "index",
  question: "What changed?",
  status: "running",
  answer: null,
  error: null,
  metrics: {},
  created_at: new Date(Date.now() - 3000).toISOString(),
  completed_at: null,
};
const generation = {
  id: "attempt-1",
  operation: "generation",
  model: "provider/actual-model",
  status: "dispatched",
  created_at: run.created_at,
};
let snapshot: Run;
let failed: boolean;
let refresh: ReturnType<typeof vi.fn>;
function activity(initial = run) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  const content = (value: Run) => (
    <QueryClientProvider client={client}>
      <RunActivity run={value} owner="owner" onRefresh={refresh} />
    </QueryClientProvider>
  );
  const view = render(content(initial));
  return { ...view, update: (value: Run) => view.rerender(content(value)) };
}
beforeEach(() => {
  failed = false;
  refresh = vi.fn();
  snapshot = {
    ...run,
    events: [
      { sequence: 1, type: "queued" },
      {
        sequence: 2,
        type: "retrieving",
        data: { private_reasoning: "never show this" },
      },
      { sequence: 3, type: "generating" },
    ],
    provider_attempts: [generation],
  };
  EventStream.instances = [];
  vi.stubGlobal("EventSource", EventStream);
  vi.stubGlobal(
    "fetch",
    vi.fn(
      async () =>
        new Response(
          JSON.stringify(
            failed
              ? { error: { message: "Answer details unavailable" } }
              : snapshot,
          ),
          { status: failed ? 503 : 200 },
        ),
    ),
  );
});
afterEach(() => vi.unstubAllGlobals());

describe("Recorded run activity", () => {
  it("follows SSE notifications and displays only supplied actions and model identity", async () => {
    activity();
    await userEvent.click(
      await screen.findByText("Writing the answer", {
        selector: "summary span",
      }),
    );
    expect(screen.getByText("provider/actual-model")).toBeTruthy();
    expect(screen.getByText("Reading practice records")).toBeTruthy();
    expect(screen.queryByText("never show this")).toBeNull();
    snapshot = {
      ...snapshot,
      provider_attempts: [
        { ...generation, status: "completed" },
        { ...generation, id: "attempt-2", operation: "support_check" },
      ],
    };
    act(() => EventStream.instances[0].dispatchEvent(new MessageEvent("run")));
    await waitFor(() =>
      expect(screen.getAllByText("Checking the answer")).toHaveLength(2),
    );
    expect(screen.getAllByRole("listitem")).toHaveLength(4);
    expect(EventStream.instances[0].url).toBe(`/api/v1/runs/${run.id}/events`);
  });

  it("reports a disconnected event stream and continues using real snapshot polling", async () => {
    activity();
    await userEvent.click(
      await screen.findByText("Writing the answer", {
        selector: "summary span",
      }),
    );
    act(() => EventStream.instances[0].onerror?.());
    expect(screen.getByText(/Live feed reconnecting/)).toBeTruthy();
    snapshot = {
      ...snapshot,
      provider_attempts: [{ ...generation, operation: "repair" }],
    };
    await waitFor(
      () => expect(screen.getAllByText("Refining the answer")).toHaveLength(2),
      { timeout: 1800 },
    );
    act(() => EventStream.instances[0].onopen?.());
    expect(screen.queryByText(/Live feed reconnecting/)).toBeNull();
  });

  it("surfaces snapshot errors and allows an explicit retry", async () => {
    failed = true;
    activity();
    await userEvent.click(
      await screen.findByText("Answer details unavailable"),
    );
    expect(screen.getByRole("alert").textContent).toContain(
      "Your answer request is still saved",
    );
    failed = false;
    await userEvent.click(
      screen.getByRole("button", { name: "Retry details" }),
    );
    await screen.findAllByText("Writing the answer");
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("fetches the final receipt and closes the event stream when history reports completion first", async () => {
    const view = activity();
    await screen.findByText("Writing the answer", { selector: "summary span" });
    snapshot = {
      ...snapshot,
      status: "completed",
      completed_at: new Date(Date.parse(run.created_at) + 5000).toISOString(),
      provider_attempts: [{ ...generation, status: "completed" }],
    };
    view.update(snapshot);
    await screen.findByText("Answer details");
    await waitFor(() =>
      expect(EventStream.instances[0].close).toHaveBeenCalledOnce(),
    );
    await userEvent.click(screen.getByText("Answer details"));
    await screen.findByText("Complete");
    expect(screen.getByText("5s")).toBeTruthy();
  });

  it("shows stopping immediately even while the last provider receipt is still dispatched", async () => {
    const view = activity();
    await screen.findByText("Writing the answer", { selector: "summary span" });
    view.update({ ...run, status: "cancelling" });
    expect(screen.getByRole("status").textContent).toBe("Stopping");
    expect(
      screen.queryByText("Writing the answer", { selector: "summary span" }),
    ).toBeNull();
    snapshot = {
      ...snapshot,
      status: "cancelled",
      completed_at: run.created_at,
    };
    act(() => EventStream.instances[0].dispatchEvent(new MessageEvent("run")));
    await screen.findByText("Answer details");
    await waitFor(() =>
      expect(EventStream.instances[0].close).toHaveBeenCalledOnce(),
    );
    expect(refresh).toHaveBeenCalled();
  });

  it("does not fetch collapsed saved activity until requested", async () => {
    snapshot = {
      ...snapshot,
      status: "completed",
      completed_at: run.created_at,
    };
    activity({ ...snapshot, events: undefined, provider_attempts: undefined });
    expect(fetch).not.toHaveBeenCalled();
    expect(EventStream.instances).toHaveLength(0);
    await userEvent.click(screen.getByText("Answer details"));
    await screen.findByText("provider/actual-model");
    expect(fetch).toHaveBeenCalledOnce();
  });

  it("closes the stream when navigating away", async () => {
    const view = activity();
    await screen.findByText("Writing the answer", { selector: "summary span" });
    view.unmount();
    expect(EventStream.instances[0].close).toHaveBeenCalledOnce();
  });

  it("shows recorded processing, record count and usage without double-counting matching receipts", async () => {
    const usage = [
      { total_tokens: 150, cost: 0.00125 },
      { prompt_tokens: 40, completion_tokens: 10, cost: 0.00025 },
    ];
    snapshot = {
      ...snapshot,
      status: "completed",
      metrics: { total_ms: 1250, source_count: 3, usage },
      provider_attempts: usage.map((value, index) => ({
        ...generation,
        id: `receipt-${index}`,
        status: "completed",
        usage: value,
      })),
    };
    activity(snapshot);
    await userEvent.click(screen.getByText("Answer details"));
    await screen.findByText("Processing time");
    expect(screen.getByText("1.25 s")).toBeTruthy();
    expect(
      screen.getByText("Records read").nextElementSibling?.textContent,
    ).toBe("3");
    expect(
      screen.getByText("Recorded calls").nextElementSibling?.textContent,
    ).toBe("2");
    expect(screen.getByText("Tokens").nextElementSibling?.textContent).toBe(
      "200",
    );
    expect(screen.getByText("Cost (USD)").nextElementSibling?.textContent).toBe(
      "$0.0015",
    );
  });

  it("labels partial receipts as a lower bound and updates them from the existing live stream", async () => {
    snapshot = {
      ...snapshot,
      provider_attempts: [
        {
          ...generation,
          status: "completed",
          usage: { total_tokens: 120, cost: 0.002 },
        },
        { ...generation, id: "unconfirmed", status: "uncertain", usage: null },
      ],
    };
    activity();
    await userEvent.click(
      await screen.findByText("Preparing the answer", {
        selector: "summary span",
      }),
    );
    expect(screen.getByText("At least 120")).toBeTruthy();
    expect(screen.getByText("At least $0.002")).toBeTruthy();
    snapshot = {
      ...snapshot,
      provider_attempts: [
        snapshot.provider_attempts![0],
        {
          ...generation,
          id: "unconfirmed",
          status: "completed",
          usage: { total_tokens: 30, cost: 0.001 },
        },
      ],
    };
    act(() => EventStream.instances[0].dispatchEvent(new MessageEvent("run")));
    await screen.findByText("$0.003");
    expect(screen.getByText("Tokens").nextElementSibling?.textContent).toBe(
      "150",
    );
  });

  it("does not turn unknown usage into zero tokens or a free request", async () => {
    snapshot = {
      ...snapshot,
      status: "failed",
      provider_attempts: [{ ...generation, status: "uncertain", usage: null }],
    };
    activity(snapshot);
    await userEvent.click(screen.getByText("Answer details"));
    expect(screen.getByText("Tokens").nextElementSibling?.textContent).toBe(
      "Not reported",
    );
    expect(screen.getByText("Cost (USD)").nextElementSibling?.textContent).toBe(
      "Not reported",
    );
    expect(screen.queryByText("$0.00")).toBeNull();
  });

  it("shows only reported failure reasons on failed answers, never arbitrary event data", async () => {
    snapshot = {
      ...snapshot,
      status: "failed",
      metrics: {
        failure_detail: [
          "Unsupported statement: review the quoted span.",
          null,
          7,
        ],
      },
    };
    activity(snapshot);
    await userEvent.click(screen.getByText("Answer details"));
    await screen.findByText("Unsupported statement: review the quoted span.");
    expect(screen.queryByText("never show this")).toBeNull();
  });

  it("keeps missing receipt usage explicit even when final metrics contain other completed calls", async () => {
    snapshot = {
      ...snapshot,
      status: "failed",
      metrics: { usage: [{ total_tokens: 20, cost: 0.004 }] },
      provider_attempts: [
        { ...generation, status: "completed" },
        { ...generation, id: "unknown", status: "uncertain" },
      ],
    };
    activity(snapshot);
    await userEvent.click(screen.getByText("Answer details"));
    expect(screen.getByText("At least 20")).toBeTruthy();
    expect(screen.getByText("At least $0.004")).toBeTruthy();
    expect(
      screen.getByText("Recorded calls").nextElementSibling?.textContent,
    ).toBe("2");
  });

  it("does not show failure details on a successful answer", async () => {
    snapshot = {
      ...snapshot,
      status: "completed",
      metrics: { failure_detail: ["Do not display a stale failure."] },
    };
    activity(snapshot);
    await userEvent.click(screen.getByText("Answer details"));
    expect(screen.queryByText("Why this answer stopped")).toBeNull();
    expect(screen.queryByText("Do not display a stale failure.")).toBeNull();
  });
  it("shows provider latency only from an actual recorded receipt", async () => {
    snapshot = {
      ...snapshot,
      status: "completed",
      provider_attempts: [
        { ...generation, status: "completed", usage: { latency_ms: 5079.766 } },
        { ...generation, id: "unknown-time", status: "uncertain", usage: null },
      ],
    };
    activity(snapshot);
    await userEvent.click(screen.getByText("Answer details"));
    expect(screen.getByText("5.08 s provider latency")).toBeTruthy();
    expect(screen.getAllByText(/provider latency/)).toHaveLength(1);
    expect(screen.queryByText("0 s provider latency")).toBeNull();
  });
});

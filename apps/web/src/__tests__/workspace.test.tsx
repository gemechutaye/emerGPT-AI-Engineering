import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import App from "../App";
import type { Answer, Conversation, Run } from "../api";
import type { useLive } from "../useLive";
import baseStyles from "../styles.css?raw";
import workspaceStyles from "../workspace.css?raw";
import pikaStyles from "../pika.css?raw";

// HTTP and Live-hook doubles: these tests do not establish real persistence,
// layout, provider, deployed, spoken, or physical-device acceptance.
const voice = vi.hoisted(() => ({
  start: vi.fn(),
  end: vi.fn(),
  onRun: undefined as ((id: string) => void) | undefined,
  transcript: [] as ReturnType<typeof useLive>["transcript"],
}));
vi.mock("../useLive", async () => {
  const { useCallback, useState } = await import("react");
  const { quietAudio } = await import("../liveAudio");
  return {
    useLive(onRun: (id: string) => void): ReturnType<typeof useLive> {
      const [state, setState] = useState<"idle" | "listening" | "ended">(
        "idle",
      );
      voice.onRun = onRun;
      const start = useCallback(async (id: string, version: number) => {
        await voice.start(id, version);
        setState("listening");
      }, []);
      const end = useCallback(async () => {
        await voice.end();
        setState((current) => (current === "idle" ? "idle" : "ended"));
      }, []);
      return {
        state,
        active: state === "listening",
        start,
        end,
        error: "",
        captions: { input: "", output: "" },
        transcript: voice.transcript,
        transcriptTruncated: false,
        audioActivity: quietAudio,
        visualizerError: "",
        muted: false,
        needsPlayback: false,
        toggleMute: vi.fn(),
        resumePlayback: vi.fn(),
      };
    },
  };
});
const answer: Answer = {
  status: "partial",
  index_id: "fixture-index",
  index_checksum: "fixture-checksum",
  statements: [
    {
      part_id: "p1",
      text: "PT-006 requested cost information before deciding.",
      citations: [
        {
          doc_id: "PT-006",
          title: "Synthetic Patient Record PT-006",
          version: "1.0",
          effective_date: "2026-09-01",
          authority: "Synthetic Patient Source",
          source_sha256: "fixture-source",
          index_id: "fixture-index",
          quote: "The patient requested cost information before deciding.",
          start: 0,
          end: 53,
        },
      ],
    },
  ],
  gaps: [
    { part_id: "p1", text: "An exact RF microneedling price is not supplied." },
  ],
  next_steps: [],
  scopes: [
    {
      part_id: "p1",
      question: "What does PT-006 need?",
      patient_ids: ["PT-006"],
      unknown_patient_ids: [],
      as_of: "2026-09-11",
      date_origin: "today",
      all_patients: false,
      warnings: [],
    },
  ],
  usage: [],
  diagnostics: {},
};
const conversation: Conversation = {
  id: "conversation-1",
  title: "Patient question",
  updated_at: "2026-09-11T00:00:00Z",
  pinned: false,
  title_origin: "auto",
  summary: null,
  summary_status: "idle",
  summary_error: null,
  context_version: 1,
  patient_id: null,
  as_of: null,
  created_at: "2026-09-11T00:00:00Z",
  runs: [],
};
const completedRun: Run = {
  id: "run-1",
  conversation_id: conversation.id,
  question: "What does PT-006 need?",
  context_version: 1,
  index_id: "fixture-index",
  status: "completed",
  answer,
  error: null,
  metrics: { elapsed_ms: 123 },
  created_at: "2026-09-11T00:00:00Z",
  completed_at: "2026-09-11T00:00:01Z",
};
type RequestRecord = {
  path: string;
  method: string;
  body: Record<string, unknown>;
};
let calls: RequestRecord[];
let savedConversation: Conversation;
let custom:
  | ((
      request: RequestRecord,
    ) => Response | undefined | Promise<Response | undefined>)
  | undefined;
function response(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}
const failure = (message: string, code = "TEMPORARY_FAILURE", status = 503) =>
  response({ error: { code, message, retryable: true } }, status);
const questionField = () =>
  screen.getByRole("textbox", { name: "Your question" }) as HTMLTextAreaElement;
const historyToggle = () =>
  screen.getByRole("button", { name: /^(Open|Close) sidebar$/ });
const generatedRuns = () =>
  calls.filter((call) => call.path.endsWith("/runs") && call.method === "POST");

function workspace({
  saved = false,
  gcTime = 0,
}: { saved?: boolean; gcTime?: number } = {}) {
  if (saved) {
    savedConversation = { ...conversation, runs: [completedRun] };
    window.localStorage.setItem("emer:conversation:session-1", conversation.id);
  }
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime },
      mutations: { retry: false },
    },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <App />
    </QueryClientProvider>,
  );
}
async function readyWorkspace() {
  await waitFor(() =>
    expect(
      (
        screen.getByRole("button", {
          name: "Voice",
          exact: true,
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(false),
  );
}
beforeEach(() => {
  calls = [];
  savedConversation = { ...conversation, runs: [] };
  custom = undefined;
  voice.start.mockReset().mockResolvedValue(undefined);
  voice.end.mockReset().mockResolvedValue(undefined);
  voice.onRun = undefined;
  voice.transcript = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string, init?: RequestInit) => {
      const request: RequestRecord = {
        path: String(input).replace("/api/v1", ""),
        method: init?.method ?? "GET",
        body: init?.body ? JSON.parse(String(init.body)) : {},
      };
      calls.push(request);
      const override = await custom?.(request);
      if (override) return override;
      if (request.path === "/session")
        return response({
          id: "session-1",
          expires_at: "2026-10-01T00:00:00Z",
        });
      if (request.path === "/conversations" && request.method === "GET")
        return response({ items: [savedConversation] });
      if (request.path === "/conversations" && request.method === "POST") {
        savedConversation = {
          ...savedConversation,
          title: String(request.body.title),
        };
        return response(savedConversation, 201);
      }
      if (
        request.path === `/conversations/${conversation.id}` &&
        request.method === "GET"
      )
        return response(savedConversation);
      if (request.path === `/conversations/${conversation.id}/runs`) {
        const run = {
          ...completedRun,
          question: String(request.body.question),
          context_version: Number(request.body.context_version),
        };
        savedConversation = { ...savedConversation, runs: [run] };
        return response(run, 202);
      }
      if (request.path === "/runs/run-1")
        return response(
          savedConversation.runs?.find((run) => run.id === "run-1") ??
            completedRun,
        );
      if (request.path === "/runs/run-1/cancel") {
        const cancelled = {
          ...completedRun,
          status: "cancelled",
          answer: null,
        };
        savedConversation = { ...savedConversation, runs: [cancelled] };
        return response(cancelled);
      }
      throw new Error(
        `Unexpected test request: ${request.method} ${request.path}`,
      );
    }),
  );
});
afterEach(() => vi.useRealTimers());

describe("Simplified workspace with HTTP and Live-hook doubles", () => {
  it.each([false, true])(
    "keeps the nonclinical disclosure visible below the composer (saved chat: %s)",
    async (saved) => {
      const styles = document.createElement("style");
      styles.textContent = `${baseStyles}\n${workspaceStyles}\n${pikaStyles}`;
      document.head.append(styles);
      try {
        workspace({ saved });
        await readyWorkspace();
        const disclosure = screen.getByText(
          "Synthetic records. Not for clinical decisions.",
        );
        const footnote = disclosure.parentElement!;
        expect(footnote.className).toBe("workspace-footnote");
        expect(footnote.previousElementSibling?.tagName).toBe("FORM");
        for (
          let element: HTMLElement | null = disclosure;
          element;
          element = element.parentElement
        ) {
          const style = window.getComputedStyle(element);
          expect(style.display).not.toBe("none");
          expect(style.visibility).not.toBe("hidden");
          expect(style.opacity).not.toBe("0");
          expect(element.hidden).toBe(false);
        }
        expect(
          screen.getAllByText("Synthetic records. Not for clinical decisions."),
        ).toHaveLength(1);
      } finally {
        styles.remove();
      }
    },
  );

  it("shows end-of-answer sources without restoring removed navigation or mount requests", async () => {
    workspace({ saved: true });
    await screen.findByText(answer.statements[0].text);
    expect(historyToggle().querySelectorAll(".brand-symbol i")).toHaveLength(3);
    const identity = screen.getByRole("img", { name: "emer-GPT" });
    expect(identity.closest(".answer-heading")).toBeTruthy();
    expect(document.querySelector(".answer-avatar")).toBeNull();
    expect(document.querySelector(".answer-reveal")).toBeNull();
    expect(historyToggle().textContent).toBe("emer-GPT");
    expect(historyToggle().getAttribute("aria-expanded")).toBe("false");
    for (const name of [/draft/i, /reset/i, /^PT-006$/, /Run details/i])
      expect(screen.queryByRole("button", { name })).toBeNull();
    expect(screen.queryByRole("combobox")).toBeNull();
    expect(screen.queryByLabelText("Policy date, optional")).toBeNull();
    expect(screen.queryByText("Run details")).toBeNull();
    const sources = screen.getByRole("region", { name: "Sources 1" });
    expect(
      within(sources).getByRole("button", { name: /^Source 1:/ }),
    ).toBeTruthy();
    expect(
      screen.getByText(answer.gaps[0].text).compareDocumentPosition(sources) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(sources.closest(".run-activity")).toBeNull();
    expect(document.querySelector(".source-drawer")).toBeNull();
    expect(
      calls.some((call) =>
        /corpus|patients|sources|drafts|evaluations|\/runs\//.test(call.path),
      ),
    ).toBe(false);
  });

  it("keeps a reader's position during updates and resumes following only on request", async () => {
    const user = userEvent.setup();
    workspace({ saved: true });
    await screen.findByText(answer.statements[0].text);
    const viewport = document.getElementById("conversation")!;
    Object.defineProperties(viewport, {
      scrollHeight: { value: 1200, configurable: true },
      clientHeight: { value: 400 },
      scrollTop: { value: 180, writable: true },
    });
    fireEvent.scroll(viewport);
    expect(
      screen.getByRole("button", { name: "Latest", exact: true }),
    ).toBeTruthy();
    await user.type(questionField(), "A follow-up I am still writing");
    expect(viewport.scrollTop).toBe(180);
    await user.click(
      screen.getByRole("button", { name: "Latest", exact: true }),
    );
    expect(viewport.scrollTop).toBe(1200);
    expect(
      screen.queryByRole("button", { name: "Latest", exact: true }),
    ).toBeNull();
  });

  it("submits without manual context changes and keeps supported statements and gaps", async () => {
    const user = userEvent.setup();
    workspace();
    await readyWorkspace();
    await user.type(questionField(), completedRun.question);
    await user.click(screen.getByRole("button", { name: "Send question" }));
    await screen.findByText(answer.statements[0].text);
    expect(
      calls.find(
        (call) => call.path === "/conversations" && call.method === "POST",
      )?.body,
    ).toEqual({ title: "New conversation" });
    expect(calls.some((call) => call.path.endsWith("/context"))).toBe(false);
    expect(generatedRuns()[0].body.context_version).toBe(1);
    expect(questionField().value).toBe("");
    expect(
      screen.getByText("An exact RF microneedling price is not supplied."),
    ).toBeTruthy();
    expect(screen.getByText("Some details missing")).toBeTruthy();
  });

  it("keeps the same intent key and question after an uncertain submission", async () => {
    let attempts = 0;
    custom = (request) =>
      request.path.endsWith("/runs") && attempts++ === 0
        ? failure("Provider unavailable.")
        : undefined;
    const user = userEvent.setup();
    workspace();
    await readyWorkspace();
    await user.type(questionField(), completedRun.question);
    await user.click(screen.getByRole("button", { name: "Send question" }));
    await screen.findByText("Provider unavailable.");
    expect(questionField().value).toBe(completedRun.question);
    await user.click(screen.getByRole("button", { name: "Send question" }));
    await screen.findByText(answer.statements[0].text);
    expect(generatedRuns()).toHaveLength(2);
    expect(generatedRuns()[0].body.idempotency_key).toBe(
      generatedRuns()[1].body.idempotency_key,
    );
  });

  it("preserves follow-up text typed during submission", async () => {
    let finish!: () => void;
    custom = (request) =>
      request.path.endsWith("/runs")
        ? new Promise((resolve) => {
            finish = () => resolve(undefined);
          })
        : undefined;
    const user = userEvent.setup();
    workspace();
    await readyWorkspace();
    await user.type(questionField(), completedRun.question);
    await user.click(screen.getByRole("button", { name: "Send question" }));
    await waitFor(() => expect(finish).toBeTypeOf("function"));
    await user.clear(questionField());
    await user.type(questionField(), "Keep this follow-up");
    finish();
    await screen.findByText(answer.statements[0].text);
    expect(questionField().value).toBe("Keep this follow-up");
    expect(generatedRuns()).toHaveLength(1);
  });

  it("explicitly retries a failed server bootstrap without losing text or leaving Voice disabled", async () => {
    let attempts = 0;
    custom = (request) =>
      request.path === "/session" && attempts++ === 0
        ? failure("Session service unavailable.")
        : undefined;
    const user = userEvent.setup();
    workspace();
    await screen.findByText("Session service unavailable.");
    await user.type(questionField(), "Keep this unsent question");
    expect(
      (screen.getByRole("button", { name: "Voice" }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
    expect(
      (
        screen.getByRole("button", {
          name: "Send question",
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(true);
    await user.click(historyToggle());
    expect(screen.getByText("Connect to see your chats.")).toBeTruthy();
    expect(screen.queryByText("No conversations yet")).toBeNull();
    await user.click(screen.getByRole("button", { name: "Retry connection" }));
    await readyWorkspace();
    expect(questionField().value).toBe("Keep this unsent question");
    expect(screen.queryByText("Session service unavailable.")).toBeNull();
    expect(
      (
        screen.getByRole("button", {
          name: "Send question",
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(false);
    await user.click(screen.getByRole("button", { name: "Voice" }));
    await waitFor(() =>
      expect(voice.start).toHaveBeenCalledWith(conversation.id, 1),
    );
    expect(calls.filter((call) => call.path === "/session")).toHaveLength(2);
    expect(generatedRuns()).toHaveLength(0);
    expect(questionField().value).toBe("Keep this unsent question");
  });

  it("opens sidebar from the logo and closes from its own header with focus return", async () => {
    const user = userEvent.setup();
    workspace();
    await readyWorkspace();
    const toggle = historyToggle();
    await user.hover(toggle);
    expect(screen.queryByRole("complementary", { name: "Sidebar" })).toBeNull();
    await user.click(toggle);
    const sidebar = screen.getByRole("complementary", { name: "Sidebar" });
    expect(
      within(sidebar).getByRole("button", { name: "New chat", exact: true }),
    ).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Open sidebar" })).toBeNull();
    expect(sidebar.id).toBe(toggle.getAttribute("aria-controls"));
    const timestamp = sidebar.querySelector("time");
    expect(timestamp?.getAttribute("datetime")).toBe(conversation.created_at);
    expect(timestamp?.textContent).toMatch(/\d:\d{2}/);
    expect(screen.queryByRole("menu")).toBeNull();
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(document.activeElement).toBe(
      screen.getByRole("button", { name: "Close sidebar", exact: true }),
    );
    await user.click(historyToggle());
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    expect(toggle.classList.contains("is-restored")).toBe(true);
    expect(screen.queryByRole("complementary", { name: "Sidebar" })).toBeNull();
    toggle.focus();
    await user.keyboard("{Enter}");
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    await user.keyboard("{Escape}");
    await waitFor(() => expect(document.activeElement).toBe(toggle));
    expect(screen.queryByRole("complementary", { name: "Sidebar" })).toBeNull();
    await user.click(toggle);
    await user.click(screen.getByRole("button", { name: "Close sidebar" }));
    await waitFor(() => expect(document.activeElement).toBe(toggle));
  });

  it("searches in a separate dialog without clearing the composer or closing the sidebar", async () => {
    const user = userEvent.setup();
    workspace();
    await readyWorkspace();
    await user.type(questionField(), "Keep this thought");
    await user.click(historyToggle());
    await user.click(screen.getByRole("button", { name: "Search chats" }));
    await user.type(
      screen.getByRole("textbox", { name: "Search all chats" }),
      "Patient",
    );
    await user.click(
      screen.getByRole("button", { name: "Close search chats" }),
    );
    expect(screen.getByRole("complementary", { name: "Sidebar" })).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "Search chats" }));
    expect(
      (
        screen.getByRole("textbox", {
          name: "Search all chats",
        }) as HTMLInputElement
      ).value,
    ).toBe("Patient");
    expect(questionField().value).toBe("Keep this thought");
    expect(generatedRuns()).toHaveLength(0);
  });

  it("distinguishes loading, failed and empty history, with an explicit retry", async () => {
    let finish!: () => void;
    let attempts = 0;
    custom = (request) => {
      if (request.path !== "/conversations" || request.method !== "GET") return;
      if (attempts++ === 0)
        return new Promise((resolve) => {
          finish = () => resolve(failure("History unavailable."));
        });
      return response({ items: [] });
    };
    const user = userEvent.setup();
    workspace();
    await readyWorkspace();
    await user.click(historyToggle());
    expect(screen.getByText("Loading chats…")).toBeTruthy();
    expect(screen.queryByText("No conversations yet")).toBeNull();
    finish();
    await screen.findByText("History unavailable.");
    expect(screen.queryByText("No conversations yet")).toBeNull();
    await user.click(screen.getByRole("button", { name: "Retry history" }));
    await screen.findByText("No conversations yet");
    expect(screen.queryByText("History unavailable.")).toBeNull();
  });

  it("searches all saved chats on the server with an honest no-match state", async () => {
    const user = userEvent.setup();
    workspace();
    custom = (request) =>
      request.path.startsWith("/conversations?q=")
        ? response({ items: [] })
        : undefined;
    await readyWorkspace();
    await user.click(historyToggle());
    await user.click(screen.getByRole("button", { name: "Search chats" }));
    await user.type(
      screen.getByRole("textbox", { name: "Search all chats" }),
      "does-not-match",
    );
    await screen.findByText("No matching chats. Try another word.");
    expect(
      within(screen.getByRole("dialog")).queryByRole("button", {
        name: /^Patient question/,
      }),
    ).toBeNull();
    await user.click(screen.getByRole("button", { name: "Clear chat search" }));
    expect(
      await within(screen.getByRole("dialog")).findByRole("button", {
        name: /^Patient question/,
      }),
    ).toBeTruthy();
    expect(generatedRuns()).toHaveLength(0);
  });

  it("paginates real conversation titles and retains loaded history after a failed next page", async () => {
    const items = Array.from({ length: 50 }, (_, i) => ({
      ...conversation,
      id: `c-${i}`,
      title: `Saved discussion ${i}`,
    }));
    let nextAttempts = 0;
    custom = (request) => {
      if (request.path === "/conversations" && request.method === "GET")
        return response({ items });
      if (request.path === "/conversations?offset=50")
        return nextAttempts++ === 0
          ? failure("Earlier history unavailable.")
          : response({
              items: [
                { ...conversation, title: "Older cancellation discussion" },
              ],
            });
    };
    const user = userEvent.setup();
    workspace();
    await readyWorkspace();
    await user.click(historyToggle());
    await user.click(
      screen.getByRole("button", { name: "Load earlier conversations" }),
    );
    await screen.findByText("Earlier history unavailable.");
    expect(
      screen.getByRole("button", { name: /^Saved discussion 49/ }),
    ).toBeTruthy();
    expect(screen.queryByText("No conversations yet")).toBeNull();
    await user.click(screen.getByRole("button", { name: "Retry history" }));
    await screen.findByRole("button", {
      name: /^Older cancellation discussion/,
    });
    expect(
      screen.queryByRole("button", { name: "Load earlier conversations" }),
    ).toBeNull();
    expect(
      calls.filter((call) => call.path === "/conversations?offset=50"),
    ).toHaveLength(2);
  });

  it("restores the selected conversation and server title across a component remount", async () => {
    const user = userEvent.setup();
    const first = workspace();
    await readyWorkspace();
    await user.type(questionField(), completedRun.question);
    await user.click(screen.getByRole("button", { name: "Send question" }));
    await screen.findByText(answer.statements[0].text);
    expect(window.localStorage.getItem("emer:conversation:session-1")).toBe(
      conversation.id,
    );
    first.unmount();
    savedConversation.title = "PT-006 Treatment Questions";
    workspace();
    await screen.findByText(answer.statements[0].text);
    expect(screen.getByRole("heading", { level: 1 }).textContent).toBe(
      "PT-006 Treatment Questions",
    );
    await user.click(historyToggle());
    const saved = screen.getByRole("button", {
      name: /^PT-006 Treatment Questions/,
    });
    expect(saved.getAttribute("aria-current")).toBe("page");
    expect(generatedRuns()).toHaveLength(1);
  });

  it("reconciles a cached running conversation after Dataset navigation without reload or resubmission", async () => {
    custom = (request) =>
      request.path === "/corpus"
        ? response({ index_id: "fixture-index", count: 0, sources: [] })
        : undefined;
    const user = userEvent.setup();
    workspace({ saved: true, gcTime: 300_000 });
    savedConversation = {
      ...conversation,
      runs: [
        {
          ...completedRun,
          status: "running",
          answer: null,
          completed_at: null,
        },
      ],
    };
    await screen.findByRole("button", { name: "Stop", exact: true });
    const readsBeforeNavigation = calls.filter(
      (call) => call.path === `/conversations/${conversation.id}`,
    ).length;
    await user.click(historyToggle());
    await user.click(
      screen.getByRole("button", { name: "New chat", exact: true }),
    );
    await user.click(
      screen.getByRole("button", { name: "Dataset", exact: true }),
    );
    await screen.findByRole("heading", { name: "The EMER dataset" });
    savedConversation = { ...conversation, runs: [completedRun] };
    await user.click(historyToggle());
    await user.click(screen.getByRole("button", { name: /^Patient question/ }));
    await screen.findByText(answer.statements[0].text);
    expect(
      screen.queryByRole("button", { name: "Stop", exact: true }),
    ).toBeNull();
    expect(screen.getByText("Answer details")).toBeTruthy();
    expect(window.location.hash).toBe("");
    expect(
      calls.filter((call) => call.path === `/conversations/${conversation.id}`)
        .length,
    ).toBeGreaterThan(readsBeforeNavigation);
    expect(generatedRuns()).toHaveLength(0);
    expect(calls.some((call) => call.path.endsWith("/cancel"))).toBe(false);
  });

  it("starts a new chat without resetting or deleting saved work", async () => {
    const user = userEvent.setup();
    workspace({ saved: true });
    await screen.findByText(answer.statements[0].text);
    window.sessionStorage.setItem(
      "emer:draft-edit:session-1:draft-1",
      "Keep saved edits",
    );
    await user.click(historyToggle());
    await user.click(
      screen.getByRole("button", { name: "New chat", exact: true }),
    );
    expect(screen.queryByText(answer.statements[0].text)).toBeNull();
    await user.click(historyToggle());
    await user.click(screen.getByRole("button", { name: /^Patient question/ }));
    await screen.findByText(answer.statements[0].text);
    expect(
      window.sessionStorage.getItem("emer:draft-edit:session-1:draft-1"),
    ).toBe("Keep saved edits");
    expect(
      calls.some(
        (call) => /reset|drafts/.test(call.path) || call.method === "DELETE",
      ),
    ).toBe(false);
    expect(generatedRuns()).toHaveLength(0);
  });

  it("recovers a failed saved-conversation read without creating a replacement or losing input", async () => {
    let reads = 0;
    custom = (request) =>
      request.path === `/conversations/${conversation.id}` && reads++ === 0
        ? failure("Conversation unavailable.")
        : undefined;
    const user = userEvent.setup();
    workspace({ saved: true });
    await screen.findByText("Conversation unavailable.");
    await user.type(questionField(), "Keep this question");
    expect(
      (screen.getByRole("button", { name: "Voice" }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
    await user.click(
      screen.getByRole("button", { name: "Reload conversation" }),
    );
    await screen.findByText(answer.statements[0].text);
    expect(questionField().value).toBe("Keep this question");
    await readyWorkspace();
    expect(
      calls.some(
        (call) => call.path === "/conversations" && call.method === "POST",
      ),
    ).toBe(false);
  });

  it("loads older answers chronologically without dispatching another generation", async () => {
    const archived = (id: string): Run => ({
      ...completedRun,
      id,
      question: `Archived question ${id}`,
      answer: null,
      status: "cancelled",
    });
    custom = (request) => {
      if (request.path === `/conversations/${conversation.id}`)
        return response({
          ...conversation,
          runs: [archived("3"), archived("4")],
          next_cursor: "3",
        });
      if (request.path === `/conversations/${conversation.id}?before=3`)
        return response({
          ...conversation,
          runs: [archived("1"), archived("2")],
          next_cursor: null,
        });
    };
    const user = userEvent.setup();
    workspace({ saved: true });
    await screen.findByRole("heading", { name: "Archived question 4" });
    await user.click(
      screen.getByRole("button", { name: "Load earlier answers" }),
    );
    await screen.findByRole("heading", { name: "Archived question 1" });
    expect(
      Array.from(document.querySelectorAll(".question-row h2")).map(
        (node) => node.textContent,
      ),
    ).toEqual(["1", "2", "3", "4"].map((id) => `Archived question ${id}`));
    expect(
      screen.getByText("Beginning of this conversation · 4 answers loaded"),
    ).toBeTruthy();
    expect(generatedRuns()).toHaveLength(0);
  });

  it("keeps operational answer failures distinct from unsupported answers", async () => {
    custom = (request) =>
      request.path === `/conversations/${conversation.id}`
        ? response({
            ...conversation,
            runs: [
              {
                ...completedRun,
                status: "failed",
                answer: null,
                error: {
                  message: "Evidence checker unavailable.",
                  code: "provider_unavailable",
                },
              },
            ],
          })
        : undefined;
    workspace({ saved: true });
    await screen.findByText("Evidence checker unavailable.");
    expect(screen.getByText("Answer could not be completed")).toBeTruthy();
    expect(screen.queryByText("Not in these records")).toBeNull();
    expect(screen.queryByText("The records don’t establish this")).toBeNull();
  });

  it.each([
    ["unsupported", "Not in these records", "Not enough information"],
    ["clarification", "Quick question", "A quick clarification"],
  ] as const)(
    "renders a truthful %s answer without a service-failure label",
    async (status, label, heading) => {
      custom = (request) =>
        request.path === `/conversations/${conversation.id}`
          ? response({
              ...conversation,
              runs: [
                {
                  ...completedRun,
                  answer: {
                    ...answer,
                    status,
                    statements: [],
                    scopes: [],
                    gaps: [
                      {
                        part_id: "p1",
                        text:
                          status === "clarification"
                            ? "Which patient should this question concern?"
                            : "No exact procedure price is supplied.",
                      },
                    ],
                  },
                },
              ],
            })
          : undefined;
      workspace({ saved: true });
      await screen.findByText(label);
      expect(screen.getByText(heading)).toBeTruthy();
      expect(screen.queryByText("Answer could not be completed")).toBeNull();
      expect(
        screen.queryByRole("button", { name: /draft|source/i }),
      ).toBeNull();
    },
  );

  it("ends centered voice from the composer toggle and restores the thread without losing text", async () => {
    const user = userEvent.setup();
    workspace({ saved: true });
    await readyWorkspace();
    await screen.findByText(answer.statements[0].text);
    await user.type(questionField(), "Keep this thought");
    const toggle = screen.getByRole("button", { name: "Voice" });
    await user.click(toggle);
    const end = await screen.findByRole("button", { name: "End voice" });
    expect(end).toBe(toggle);
    expect(end.closest(".composer")).toBeTruthy();
    expect(
      screen
        .getByRole("button", { name: "Mute microphone" })
        .closest(".composer"),
    ).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Send question" })).toBeNull();
    questionField().focus();
    await user.keyboard("{Enter}");
    expect(generatedRuns()).toHaveLength(0);
    expect(
      document.querySelector(".voice-stage .voice-session.is-live"),
    ).toBeTruthy();
    expect(
      screen.queryByRole("button", { name: "End", exact: true }),
    ).toBeNull();
    const voiceSurface = document.getElementById("voice-controls");
    expect(
      document.querySelector(".conversation-content")?.hasAttribute("hidden"),
    ).toBe(true);
    await user.click(end);
    expect(voice.end).toHaveBeenCalledOnce();
    expect(
      document.querySelector(".conversation-content")?.hasAttribute("hidden"),
    ).toBe(false);
    expect(document.getElementById("voice-controls")).toBe(voiceSurface);
    expect(screen.getByRole("button", { name: "Voice" })).toBe(toggle);
    expect(questionField().value).toBe("Keep this thought");
    expect(screen.getByText(answer.statements[0].text)).toBeTruthy();
  });

  it("offers recap creation for an existing typed chat after its recap format is retired", async () => {
    workspace({ saved: true });
    await screen.findByText(answer.statements[0].text);
    await userEvent.click(
      screen.getByText("Conversation recap", { selector: "summary" }),
    );
    expect(screen.getByRole("button", { name: "Create recap" })).toBeTruthy();
    expect(screen.getAllByText(answer.statements[0].text)).toHaveLength(1);
  });

  it("updates Recents from generated conversation metadata even when the list response stays stale", async () => {
    custom = (request) =>
      request.method === "GET" && request.path.startsWith("/conversations?")
        ? response({ items: [conversation], next_offset: null })
        : undefined;
    workspace({ saved: true });
    savedConversation = { ...savedConversation, summary_status: "generating" };
    await readyWorkspace();
    await userEvent.click(historyToggle());
    await screen.findByRole("button", { name: /^Patient question/ });
    savedConversation = {
      ...savedConversation,
      title: "Recovery Questions and Unconfirmed Costs",
      summary_status: "ready",
      summary: "The discussion covered recovery.",
      updated_at: "2026-09-12T00:00:00Z",
    };
    act(() => voice.onRun?.("run-1"));
    await screen.findByRole("heading", { name: savedConversation.title });
    expect(
      await within(screen.getByRole("complementary")).findByText(
        savedConversation.title,
      ),
    ).toBeTruthy();
    expect(
      screen.getByRole("heading", { name: savedConversation.title }),
    ).toBeTruthy();
  });

  it("retains inherited and previous context without reintroducing selectors", async () => {
    custom = (request) =>
      request.path === `/conversations/${conversation.id}`
        ? response({
            ...conversation,
            context_version: 2,
            patient_id: "PT-006",
            as_of: "2026-03-01",
            runs: [completedRun],
          })
        : undefined;
    workspace({ saved: true });
    await screen.findByText(/^Saved context: PT-006/);
    expect(
      screen.getByText("1 earlier answer in a different context"),
    ).toBeTruthy();
    expect(screen.queryByRole("combobox")).toBeNull();
    expect(calls.some((call) => call.path.endsWith("/context"))).toBe(false);
  });

  it("cancels an active answer and refreshes its saved status without generating again", async () => {
    workspace({ saved: true });
    savedConversation = {
      ...conversation,
      runs: [{ ...completedRun, status: "running", answer: null }],
    };
    const user = userEvent.setup();
    await user.click(
      await screen.findByRole("button", { name: "Stop", exact: true }),
    );
    await screen.findByText(
      "This answer was stopped. Your question is saved above.",
    );
    expect(
      calls.filter((call) => call.path === "/runs/run-1/cancel"),
    ).toHaveLength(1);
    expect(generatedRuns()).toHaveLength(0);
  });

  it("surfaces cancellation failure and keeps the pending answer actionable", async () => {
    custom = (request) =>
      request.path === "/runs/run-1/cancel"
        ? failure("Could not reach the cancellation service.")
        : undefined;
    workspace({ saved: true });
    savedConversation = {
      ...conversation,
      runs: [{ ...completedRun, status: "running", answer: null }],
    };
    const user = userEvent.setup();
    await user.click(
      await screen.findByRole("button", { name: "Stop", exact: true }),
    );
    await screen.findByText("Could not reach the cancellation service.");
    expect(
      (
        screen.getByRole("button", {
          name: "Stop",
          exact: true,
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(false);
    expect(screen.queryByText("Not in these records")).toBeNull();
    expect(generatedRuns()).toHaveLength(0);
  });

  it("clears expired ownership from the page, stops Live and reconnects without destroying local work", async () => {
    const user = userEvent.setup();
    workspace({ saved: true });
    await screen.findByText(answer.statements[0].text);
    window.sessionStorage.setItem(
      "emer:draft-edit:session-1:draft-1",
      "Keep saved edits",
    );
    await user.type(questionField(), "Unsent recovery question");
    await user.click(screen.getByRole("button", { name: "Voice" }));
    await waitFor(() => expect(voice.start).toHaveBeenCalledOnce());
    custom = (request) =>
      request.path === "/session"
        ? response({ id: "session-2", expires_at: "2026-10-01T00:00:00Z" })
        : request.path === "/conversations"
          ? response({ items: [] })
          : undefined;
    fireEvent(window, new Event("emer:session-expired"));
    await screen.findByText("This browser session has ended");
    expect(voice.end).toHaveBeenCalled();
    expect(screen.queryByText(answer.statements[0].text)).toBeNull();
    expect(questionField().value).toBe("Unsent recovery question");
    await user.click(
      screen.getByRole("button", { name: "Reconnect", exact: true }),
    );
    await readyWorkspace();
    expect(screen.queryByText(answer.statements[0].text)).toBeNull();
    expect(
      window.sessionStorage.getItem("emer:draft-edit:session-1:draft-1"),
    ).toBe("Keep saved edits");
    expect(calls.some((call) => call.path === "/session/reset")).toBe(false);
  });

  it("handles actual HTTP session expiry during history pagination rather than showing old ownership", async () => {
    custom = (request) => {
      if (request.path === "/conversations")
        return response({
          items: Array.from({ length: 50 }, (_, i) => ({
            ...conversation,
            id: `c-${i}`,
          })),
        });
      if (request.path === "/conversations?offset=50")
        return failure("The session has expired.", "SESSION_EXPIRED", 401);
    };
    const user = userEvent.setup();
    workspace();
    await readyWorkspace();
    await user.click(historyToggle());
    await user.click(
      screen.getByRole("button", { name: "Load earlier conversations" }),
    );
    await screen.findByText("This browser session has ended");
    expect(
      screen.queryByRole("button", { name: /Patient question/ }),
    ).toBeNull();
    expect(screen.getByText("Connect to see your chats.")).toBeTruthy();
  });

  it("responds to another tab's ownership reset without sending a reset itself", async () => {
    workspace({ saved: true });
    await screen.findByText(answer.statements[0].text);
    fireEvent(
      window,
      new StorageEvent("storage", {
        key: "emer:session-reset",
        newValue: "next",
      }),
    );
    await screen.findByText("This browser session has ended");
    expect(screen.queryByText(answer.statements[0].text)).toBeNull();
    expect(calls.some((call) => call.path === "/session/reset")).toBe(false);
  });

  it("does not let a late submission clear text or restore a chat after New chat", async () => {
    let finish!: () => void;
    custom = (request) =>
      request.path.endsWith("/runs")
        ? new Promise((resolve) => {
            finish = () => resolve(undefined);
          })
        : undefined;
    const user = userEvent.setup();
    workspace();
    await readyWorkspace();
    await user.type(questionField(), completedRun.question);
    await user.click(screen.getByRole("button", { name: "Send question" }));
    await waitFor(() => expect(finish).toBeTypeOf("function"));
    await user.click(historyToggle());
    await user.click(
      screen.getByRole("button", { name: "New chat", exact: true }),
    );
    await user.type(questionField(), "A different conversation");
    await act(async () => finish());
    expect(questionField().value).toBe("A different conversation");
    expect(screen.queryByText(answer.statements[0].text)).toBeNull();
    expect(
      window.localStorage.getItem("emer:conversation:session-1"),
    ).toBeNull();
  });

  it("starts Voice with unsent text without submitting or using that text as a title", async () => {
    const user = userEvent.setup();
    workspace();
    await readyWorkspace();
    await user.type(questionField(), "Do not send this typed question");
    await user.click(screen.getByRole("button", { name: "Voice" }));
    await waitFor(() =>
      expect(voice.start).toHaveBeenCalledWith(conversation.id, 1),
    );
    expect(questionField().value).toBe("Do not send this typed question");
    expect(generatedRuns()).toHaveLength(0);
    expect(
      calls.find(
        (call) => call.path === "/conversations" && call.method === "POST",
      )?.body,
    ).toEqual({ title: "Voice conversation" });
  });

  it("keeps Voice actionable during a running text answer", async () => {
    const user = userEvent.setup();
    workspace({ saved: true });
    savedConversation = {
      ...conversation,
      runs: [{ ...completedRun, status: "running", answer: null }],
    };
    await screen.findByRole("button", { name: "Stop", exact: true });
    await user.type(questionField(), "Keep my follow-up");
    await user.click(screen.getByRole("button", { name: "Voice" }));
    await waitFor(() =>
      expect(voice.start).toHaveBeenCalledWith(conversation.id, 1),
    );
    expect(questionField().value).toBe("Keep my follow-up");
    expect(generatedRuns()).toHaveLength(0);
    expect(calls.some((call) => call.path.endsWith("/cancel"))).toBe(false);
  });

  it("shares a pending conversation creation when Voice starts during text submission", async () => {
    voice.transcript = [
      {
        id: "spoken-1",
        speaker: "input",
        text: "A separate spoken question",
        start: 0,
        end: 1,
      },
    ];
    let finish!: () => void;
    custom = (request) =>
      request.path === "/conversations" && request.method === "POST"
        ? new Promise((resolve) => {
            finish = () => resolve(undefined);
          })
        : undefined;
    const user = userEvent.setup();
    workspace();
    await readyWorkspace();
    await user.type(questionField(), completedRun.question);
    await user.click(screen.getByRole("button", { name: "Send question" }));
    await waitFor(() => expect(finish).toBeTypeOf("function"));
    await user.click(screen.getByRole("button", { name: "Voice" }));
    await user.clear(questionField());
    await user.type(questionField(), "Keep this separate thought");
    finish();
    await waitFor(() =>
      expect(voice.start).toHaveBeenCalledWith(conversation.id, 1),
    );
    await screen.findByText(answer.statements[0].text);
    expect(
      calls.filter(
        (call) => call.path === "/conversations" && call.method === "POST",
      ),
    ).toHaveLength(1);
    expect(generatedRuns()).toHaveLength(1);
    expect(questionField().value).toBe("Keep this separate thought");
    await user.click(
      screen.getByRole("button", { name: "End voice", exact: true }),
    );
    await screen.findByText(/Checking for your saved conversation/);
    expect(screen.queryByText(/Answer saved in this chat/)).toBeNull();
  });

  it("keeps an unsent draft through the Inside tour and browser Back", async () => {
    custom = (request) =>
      request.path === "/corpus"
        ? response({ index_id: "fixture-index", count: 0, sources: [] })
        : undefined;
    const user = userEvent.setup();
    workspace();
    await readyWorkspace();
    await user.type(questionField(), "Keep this question for later");
    await user.click(
      screen.getByRole("button", { name: "Dataset", exact: true }),
    );
    expect(window.location.hash).toBe("#inside");
    expect(screen.queryByRole("textbox", { name: "Your question" })).toBeNull();
    expect(document.querySelector(".workspace")?.hasAttribute("inert")).toBe(
      true,
    );
    await act(async () => {
      window.history.replaceState(null, "", window.location.pathname);
      window.dispatchEvent(new PopStateEvent("popstate"));
    });
    expect(questionField().value).toBe("Keep this question for later");
    expect(generatedRuns()).toHaveLength(0);
  });

  it("ends Voice preparation from Inside before a late conversation can start audio", async () => {
    let finish!: () => void;
    custom = (request) =>
      request.path === "/corpus"
        ? response({ index_id: "fixture-index", count: 0, sources: [] })
        : request.path === "/conversations" && request.method === "POST"
          ? new Promise((resolve) => {
              finish = () => resolve(undefined);
            })
          : undefined;
    const user = userEvent.setup();
    workspace();
    await readyWorkspace();
    await user.click(
      screen.getByRole("button", { name: "Voice", exact: true }),
    );
    await waitFor(() => expect(finish).toBeTypeOf("function"));
    await user.click(
      screen.getByRole("button", { name: "Dataset", exact: true }),
    );
    await user.click(
      screen.getByRole("button", { name: "End voice", exact: true }),
    );
    await act(async () => finish());
    expect(voice.start).not.toHaveBeenCalled();
    expect(generatedRuns()).toHaveLength(0);
    await user.click(
      screen.getByRole("button", { name: "Dataset", exact: true }),
    );
    expect(window.location.hash).toBe("");
  });

  it("fences a late conversation response when ownership expires during Voice preparation", async () => {
    let finish!: () => void;
    custom = (request) =>
      request.path === "/conversations" && request.method === "POST"
        ? new Promise((resolve) => {
            finish = () => resolve(undefined);
          })
        : undefined;
    const user = userEvent.setup();
    workspace();
    await readyWorkspace();
    await user.click(screen.getByRole("button", { name: "Voice" }));
    await waitFor(() => expect(finish).toBeTypeOf("function"));
    fireEvent(window, new Event("emer:session-expired"));
    await act(async () => finish());
    await screen.findByText("This browser session has ended");
    expect(voice.start).not.toHaveBeenCalled();
    expect(generatedRuns()).toHaveLength(0);
    expect(
      window.localStorage.getItem("emer:conversation:session-1"),
    ).toBeNull();
  });

  it("keeps polling for 90 seconds after End even after an earlier answer appears", async () => {
    const user = userEvent.setup();
    workspace({ saved: true });
    await screen.findByText(answer.statements[0].text);
    await user.click(screen.getByRole("button", { name: "Voice" }));
    await waitFor(() => expect(voice.start).toHaveBeenCalledOnce());
    vi.useFakeTimers();
    await act(async () =>
      fireEvent.click(
        screen.getByRole("button", { name: "End voice", exact: true }),
      ),
    );
    const next = {
      ...completedRun,
      id: "voice-run-1",
      question: "First spoken question",
    };
    savedConversation = { ...savedConversation, runs: [completedRun, next] };
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2800);
    });
    expect(screen.getByRole("heading", { name: next.question })).toBeTruthy();
    const final = {
      ...completedRun,
      id: "voice-run-2",
      question: "Last spoken question",
    };
    savedConversation = {
      ...savedConversation,
      runs: [completedRun, next, final],
    };
    await act(async () => {
      await vi.advanceTimersByTimeAsync(4200);
    });
    expect(screen.getByRole("heading", { name: final.question })).toBeTruthy();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(84000);
    });
    const reads = calls.filter(
      (call) => call.path === `/conversations/${conversation.id}`,
    ).length;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(4200);
    });
    expect(
      calls.filter((call) => call.path === `/conversations/${conversation.id}`),
    ).toHaveLength(reads);
    expect(generatedRuns()).toHaveLength(0);
  });

  it("uses Live run notifications to refresh saved answers without submitting new runs", async () => {
    const user = userEvent.setup();
    workspace();
    await readyWorkspace();
    await user.click(screen.getByRole("button", { name: "Voice" }));
    await waitFor(() => expect(voice.start).toHaveBeenCalledOnce());
    savedConversation = { ...savedConversation, runs: [completedRun] };
    act(() => voice.onRun?.("run-1"));
    await screen.findByText(answer.statements[0].text);
    expect(calls.some((call) => call.path === "/runs/run-1")).toBe(true);
    expect(generatedRuns()).toHaveLength(0);
  });

  it("keeps a known pending answer polling after the 90-second End watch expires", async () => {
    const user = userEvent.setup();
    workspace();
    await readyWorkspace();
    await user.click(screen.getByRole("button", { name: "Voice" }));
    await waitFor(() => expect(voice.start).toHaveBeenCalledOnce());
    savedConversation = {
      ...savedConversation,
      runs: [{ ...completedRun, status: "running", answer: null }],
    };
    vi.useFakeTimers();
    await act(async () =>
      fireEvent.click(
        screen.getByRole("button", { name: "End voice", exact: true }),
      ),
    );
    await act(async () => {
      await vi.advanceTimersByTimeAsync(91000);
    });
    const reads = calls.filter(
      (call) => call.path === `/conversations/${conversation.id}`,
    ).length;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2800);
    });
    expect(
      calls.filter((call) => call.path === `/conversations/${conversation.id}`)
        .length,
    ).toBeGreaterThan(reads);
    savedConversation = { ...savedConversation, runs: [completedRun] };
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2800);
    });
    expect(screen.getByText(answer.statements[0].text)).toBeTruthy();
    expect(generatedRuns()).toHaveLength(0);
  });
});

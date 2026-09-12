import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ConversationReview } from "../ConversationReview";
import type { Citation, Conversation, Run } from "../api";

const conversation: Conversation = {
  id: "review-chat",
  title: "Recovery questions",
  patient_id: null,
  as_of: null,
  context_version: 2,
  created_at: "2026-09-12T01:00:00Z",
  updated_at: "2026-09-12T01:05:00Z",
  pinned: false,
  title_origin: "auto",
  summary_status: "ready",
  summary_error: null,
  summary: "You discussed recovery.\n- PT-006 came up in the conversation.",
};
const quote = "Cost information was requested.";
const citation: Citation = {
  doc_id: "PT-006",
  title: "Synthetic Patient Record PT-006",
  index_id: "saved-index",
  version: "1.0",
  source_sha256: "saved-hash",
  effective_date: "2026-09-01",
  authority: "Synthetic Patient Source",
  start: 0,
  end: quote.length,
  quote,
};
const run: Run = {
  id: "checked-run",
  conversation_id: conversation.id,
  question: "What information did this patient request?",
  context_version: 1,
  index_id: citation.index_id,
  status: "completed",
  created_at: conversation.created_at,
  completed_at: conversation.updated_at,
  error: null,
  metrics: {},
  answer: {
    status: "partial",
    index_id: citation.index_id,
    index_checksum: "saved-checksum",
    statements: [{ part_id: "p1", text: quote, citations: [citation] }],
    gaps: [
      { part_id: "p2", text: "The exact procedure price is not supplied." },
    ],
    next_steps: [
      {
        part_id: "p1",
        text: "Clarify the requested cost information.",
        citations: [citation],
      },
    ],
    scopes: [],
    usage: [],
    diagnostics: {},
  },
};
function show(value = conversation, runs?: readonly Run[], collapsed = false) {
  return render(
    <QueryClientProvider
      client={
        new QueryClient({ defaultOptions: { queries: { retry: false } } })
      }
    >
      <ConversationReview
        conversation={value}
        runs={runs}
        collapsed={collapsed}
        transcript={<p>The full two-sided transcript is preserved.</p>}
        onRefresh={() => {}}
      />
    </QueryClientProvider>,
  );
}
beforeEach(() => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () =>
      Response.json({
        ...citation,
        text: quote,
        category: "synthetic_patient",
        sha256: citation.source_sha256,
      }),
    ),
  );
});

describe("Recap review sources", () => {
  it("allows a retired typed recap to be recreated from saved answers", async () => {
    show(
      { ...conversation, summary: null, summary_status: "idle", runs: [run] },
      [run],
      true,
    );
    await userEvent.click(
      screen.getByText("Conversation recap", { selector: "summary" }),
    );
    await userEvent.click(screen.getByRole("button", { name: "Create recap" }));
    expect(fetch).toHaveBeenCalledWith(
      "/api/v1/conversations/review-chat/summary",
      expect.objectContaining({ method: "POST" }),
    );
    expect(screen.queryByText(run.question)).toBeNull();
  });
  it("keeps a failed typed recap optional without duplicating saved answers", async () => {
    show(
      {
        ...conversation,
        summary: null,
        summary_status: "failed",
        summary_error: "The recap could not be verified.",
      },
      [run],
      true,
    );
    const disclosure = screen.getByText("Conversation recap · unavailable");
    expect(disclosure.closest("details")?.open).toBe(false);
    expect(screen.queryByText(run.question)).toBeNull();
    await userEvent.click(disclosure);
    expect(disclosure.closest("details")?.open).toBe(true);
    expect(screen.getByRole("button", { name: "Retry recap" })).toBeTruthy();
  });
  it("preserves the generated recap and relates original sources to the actual checked question and answer", async () => {
    show(conversation, [run]);
    expect(screen.getByText("You discussed recovery.")).toBeTruthy();
    expect(
      screen.getByText("PT-006 came up in the conversation."),
    ).toBeTruthy();
    const sources = screen.getByRole("region", {
      name: "Sources from checked answers",
    });
    expect(within(sources).getByText("You asked")).toBeTruthy();
    const question = within(sources).getByText(run.question);
    await userEvent.click(question);
    expect(
      within(sources).getByRole("heading", { name: "emer-GPT answered" }),
    ).toBeTruthy();
    expect(within(sources).getByText(quote)).toBeTruthy();
    expect(within(sources).getByText("Still unknown")).toBeTruthy();
    expect(within(sources).getByText(run.answer!.gaps[0].text)).toBeTruthy();
    expect(within(sources).getByText("Next steps")).toBeTruthy();
    expect(
      within(sources).getByText(run.answer!.next_steps[0].text),
    ).toBeTruthy();
    expect(fetch).not.toHaveBeenCalled();
    const source = within(sources).getByRole("button", { name: /^Source 1:/ });
    await userEvent.click(source);
    await screen.findByRole("heading", { name: "Original record" });
    expect(fetch).toHaveBeenCalledWith(
      "/api/v1/sources/PT-006?index_id=saved-index",
      expect.anything(),
    );
    expect(document.querySelector("mark")?.textContent).toBe(quote);
    await userEvent.click(
      screen.getByRole("button", { name: "Close source reader" }),
    );
    expect(document.activeElement).toBe(source);
  });

  it("includes cited next steps and prior contexts from this conversation once, without mixing incomplete or other conversations", () => {
    show(conversation, [
      run,
      run,
      {
        ...run,
        id: "other-chat",
        conversation_id: "other-conversation",
        question: "Other conversation",
      },
      {
        ...run,
        id: "cancelled",
        status: "cancelled",
        question: "Cancelled response",
      },
      {
        ...run,
        id: "working",
        status: "validating",
        question: "Unchecked response",
      },
      { ...run, id: "empty", answer: null, question: "No answer" },
      {
        ...run,
        id: "uncited",
        question: "Uncited answer",
        answer: { ...run.answer!, statements: [], next_steps: [] },
      },
      {
        ...run,
        id: "next-steps",
        question: "Cited next step",
        answer: { ...run.answer!, statements: [] },
      },
    ]);
    const sources = screen.getByRole("region", {
      name: "Sources from checked answers",
    });
    expect(within(sources).getAllByText(run.question)).toHaveLength(1);
    expect(within(sources).getByText("Cited next step")).toBeTruthy();
    for (const question of [
      "Other conversation",
      "Cancelled response",
      "Unchecked response",
      "No answer",
      "Uncited answer",
    ])
      expect(within(sources).queryByText(question)).toBeNull();
    expect(fetch).not.toHaveBeenCalled();
  });

  it("does not infer citations from a source identifier in the summary", () => {
    show();
    expect(
      screen.getByText("PT-006 came up in the conversation."),
    ).toBeTruthy();
    expect(
      screen.queryByRole("region", { name: "Sources from checked answers" }),
    ).toBeNull();
    expect(screen.queryByRole("button", { name: /^Source/ })).toBeNull();
    expect(fetch).not.toHaveBeenCalled();
  });

  it("uses conversation-provided runs and keeps source disclosure state through keyboard tab changes", async () => {
    show({ ...conversation, runs: [run] });
    const question = screen.getByText(run.question);
    await userEvent.click(question);
    const disclosure = question.closest("details")!;
    expect(disclosure.open).toBe(true);
    const recap = screen.getByRole("tab", { name: "Recap" });
    recap.focus();
    await userEvent.keyboard("{End}");
    const transcript = screen.getByRole("tabpanel", { name: "Transcript" });
    expect(transcript.getAttribute("tabindex")).toBe("0");
    expect(transcript.textContent).toContain("full two-sided transcript");
    await userEvent.keyboard("{Tab}");
    expect(document.activeElement).toBe(transcript);
    screen.getByRole("tab", { name: "Transcript" }).focus();
    await userEvent.keyboard("{Home}");
    expect(screen.getByRole("tabpanel", { name: "Recap" })).toBeTruthy();
    expect(disclosure.open).toBe(true);
  });
});

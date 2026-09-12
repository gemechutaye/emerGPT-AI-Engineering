import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, expect, it, vi } from "vitest";
import { ConversationRecap } from "../ConversationRecap";
import { ConversationReview } from "../ConversationReview";
import { syncConversationLists } from "../conversationCache";
import { SharedChat } from "../SharedChat";
import { TranscriptArchive } from "../TranscriptArchive";
import type { Conversation, SavedTranscript } from "../api";

const conversation: Conversation = {
  id: "chat-1",
  title: "Recovery plans",
  patient_id: null,
  as_of: null,
  context_version: 1,
  created_at: "2026-09-11T00:00:00Z",
  updated_at: "2026-09-11T00:00:00Z",
  pinned: false,
  title_origin: "auto",
  summary: null,
  summary_status: "idle",
  summary_error: null,
};
function show(ui: React.ReactNode) {
  return render(
    <QueryClientProvider
      client={
        new QueryClient({
          defaultOptions: {
            queries: { retry: false },
            mutations: { retry: false },
          },
        })
      }
    >
      {ui}
    </QueryClientProvider>,
  );
}
describe("Conversation presentation", () => {
  it("restores both transcript sides, joins fragments and keeps separate sessions ordered through pagination", async () => {
    const fragment: SavedTranscript = {
      event_id: "1",
      live_session_id: "voice-1",
      speaker: "user",
      delta: "What about ",
      start_ms: 100,
      end_ms: 200,
      created_at: conversation.created_at,
    };
    const first = [
      fragment,
      {
        ...fragment,
        event_id: "2",
        delta: "recovery?",
        start_ms: 200,
        end_ms: 300,
      },
      {
        ...fragment,
        event_id: "3",
        speaker: "assistant" as const,
        delta: "The duration remains unconfirmed.",
        start_ms: 400,
      },
    ];
    const second = [
      {
        ...fragment,
        event_id: "4",
        live_session_id: "voice-2",
        delta: "A later voice conversation.",
        start_ms: 0,
        created_at: "2026-09-12T00:00:00Z",
      },
    ];
    let fail = true;
    const fetch = vi.fn<typeof globalThis.fetch>(async (input) => {
      if (String(input).endsWith("?after=3"))
        return fail
          ? Response.json(
              { error: { message: "Transcript page unavailable." } },
              { status: 503 },
            )
          : Response.json({ items: second, next_cursor: null });
      return Response.json({ items: first, next_cursor: 3 });
    });
    vi.stubGlobal("fetch", fetch);
    show(
      <TranscriptArchive
        owner="owner"
        conversation={{
          ...conversation,
          transcripts: first,
          transcripts_next_cursor: 3,
        }}
      />,
    );
    expect(screen.getByText("What about recovery?")).toBeTruthy();
    expect(screen.getByText("The duration remains unconfirmed.")).toBeTruthy();
    await userEvent.click(
      screen.getByRole("button", { name: "Load more transcript" }),
    );
    await screen.findByText("Transcript page unavailable.");
    expect(screen.getByText("What about recovery?")).toBeTruthy();
    fail = false;
    await userEvent.click(
      screen.getByRole("button", { name: "Retry transcript" }),
    );
    await screen.findByText("A later voice conversation.");
    expect(
      screen.getAllByRole("region", { name: "Saved voice session" }),
    ).toHaveLength(2);
    expect(screen.getAllByText("What about recovery?")).toHaveLength(1);
    expect(
      screen.queryByRole("button", { name: "Load more transcript" }),
    ).toBeNull();
  });
  it("offers a deliberate recap action for older saved voice without eager generation", async () => {
    const fetch = vi.fn<typeof globalThis.fetch>(async () =>
      Response.json({ ...conversation, summary_status: "pending" }),
    );
    vi.stubGlobal("fetch", fetch);
    show(
      <ConversationRecap
        conversation={{
          ...conversation,
          transcripts: [
            {
              event_id: "1",
              live_session_id: "voice-1",
              speaker: "user",
              delta: "A saved discussion.",
              start_ms: 0,
              end_ms: 10,
              created_at: conversation.created_at,
            },
          ],
        }}
        onRefresh={() => {}}
      />,
    );
    expect(fetch).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Create recap" }));
    expect(fetch).toHaveBeenCalledOnce();
  });
  it("does not fabricate a recap for an idle conversation or infer on mount", () => {
    const fetch = vi.fn();
    vi.stubGlobal("fetch", fetch);
    show(
      <ConversationRecap conversation={conversation} onRefresh={() => {}} />,
    );
    expect(screen.queryByRole("region")).toBeNull();
    expect(fetch).not.toHaveBeenCalled();
  });
  it("distinguishes a pending recap from saved text", () => {
    show(
      <ConversationRecap
        conversation={{
          ...conversation,
          summary_status: "generating",
          summary: "The prior discussion covered recovery.",
        }}
        onRefresh={() => {}}
      />,
    );
    expect(
      screen.getByText("The prior discussion covered recovery."),
    ).toBeTruthy();
    expect(screen.getByRole("status").textContent).toContain(
      "Preparing your recap",
    );
    expect(
      screen.getByText("Conversation summary · not clinical advice"),
    ).toBeTruthy();
  });
  it("switches accessible recap/transcript tabs without discarding either panel", async () => {
    const recap = {
      ...conversation,
      summary_status: "ready" as const,
      summary:
        "The discussion covered recovery planning.\n- You asked about duration.\n- The assistant said it remains unconfirmed.",
    };
    show(
      <ConversationReview
        conversation={recap}
        transcript={<p>Both sides of the saved conversation.</p>}
        onRefresh={() => {}}
      />,
    );
    expect(screen.getByRole("tabpanel", { name: "Recap" })).toBeTruthy();
    expect(screen.getAllByRole("listitem")).toHaveLength(2);
    expect(screen.queryByRole("tabpanel", { name: "Transcript" })).toBeNull();
    screen.getByRole("tab", { name: "Recap" }).focus();
    await userEvent.keyboard("{ArrowRight}");
    expect(document.activeElement).toBe(
      screen.getByRole("tab", { name: "Transcript" }),
    );
    expect(
      screen.getByRole("tabpanel", { name: "Transcript" }).textContent,
    ).toContain("Both sides");
    expect(
      screen.getByText("The discussion covered recovery planning."),
    ).toBeTruthy();
    await userEvent.keyboard("{Home}");
    expect(
      screen.getByRole("tab", { name: "Recap" }).getAttribute("aria-selected"),
    ).toBe("true");
  });
  it("synchronizes generated titles into recent and search pages without leaking owners or overwriting newer edits", () => {
    const client = new QueryClient();
    const page = {
      pages: [{ items: [conversation], next_offset: 50 }],
      pageParams: [0],
    };
    client.setQueryData(["owner", "conversations"], page);
    client.setQueryData(["owner", "chat-search", "recovery"], page);
    client.setQueryData(["different-owner", "conversations"], page);
    const updated = {
      ...conversation,
      title: "Recovery Duration Still Unconfirmed",
      summary_status: "ready" as const,
      updated_at: "2026-09-12T00:00:00Z",
    };
    syncConversationLists(client, "owner", updated);
    for (const key of [
      ["owner", "conversations"],
      ["owner", "chat-search", "recovery"],
    ]) {
      const cached = client.getQueryData<typeof page>(key)!;
      expect(cached.pages[0].items[0].title).toBe(updated.title);
      expect(cached.pages[0].next_offset).toBe(50);
      expect(cached.pageParams).toEqual([0]);
    }
    expect(
      client.getQueryData<typeof page>(["different-owner", "conversations"])!
        .pages[0].items[0].title,
    ).toBe(conversation.title);
    syncConversationLists(client, "owner", conversation);
    expect(
      client.getQueryData<typeof page>(["owner", "conversations"])!.pages[0]
        .items[0].title,
    ).toBe(updated.title);
  });
  it("requires an explicit retry after metadata generation fails", async () => {
    const fetch = vi.fn<typeof globalThis.fetch>(async () =>
      Response.json({ status: "pending" }),
    );
    vi.stubGlobal("fetch", fetch);
    const refresh = vi.fn();
    show(
      <ConversationRecap
        conversation={{
          ...conversation,
          summary_status: "failed",
          summary_error: "The recap provider is unavailable.",
        }}
        onRefresh={refresh}
      />,
    );
    expect(fetch).not.toHaveBeenCalled();
    expect(screen.getByRole("alert").textContent).toContain(
      "provider is unavailable",
    );
    await userEvent.click(screen.getByRole("button", { name: "Retry recap" }));
    expect(fetch.mock.calls[0]?.[0]).toBe(
      "/api/v1/conversations/chat-1/summary",
    );
    expect(refresh).toHaveBeenCalledOnce();
  });
  it("loads the public snapshot without requesting private session/history or exposing controls", async () => {
    const fetch = vi.fn<typeof globalThis.fetch>(async () =>
      Response.json({
        title: "Recovery plans",
        summary: "Cost remains uncertain.",
        messages: [
          { role: "user", text: "What remains unresolved?" },
          {
            role: "assistant",
            text: "The cost and downtime are not confirmed.",
          },
        ],
      }),
    );
    vi.stubGlobal("fetch", fetch);
    show(<SharedChat token="shared-token" />);
    await screen.findByRole("heading", { name: "Recovery plans" });
    expect(
      screen.getByText("The cost and downtime are not confirmed."),
    ).toBeTruthy();
    expect(screen.queryByRole("textbox")).toBeNull();
    expect(
      screen.queryByRole("button", { name: /Voice|New chat|Source/ }),
    ).toBeNull();
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(fetch.mock.calls[0]?.[0]).toBe("/api/v1/shared/shared-token");
  });
  it("shows a revoked share failure rather than an empty successful chat", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        Response.json(
          { error: { message: "This link is no longer available." } },
          { status: 404 },
        ),
      ),
    );
    show(<SharedChat token="revoked-token" />);
    await screen.findByRole("heading", {
      name: "This conversation isn’t available",
    });
    expect(screen.getByRole("alert").textContent).toContain(
      "no longer available",
    );
  });
});

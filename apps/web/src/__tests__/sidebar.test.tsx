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
import { beforeEach, describe, expect, it, vi } from "vitest";
import { Sidebar } from "../Sidebar";
import type { Conversation } from "../api";

const original: Conversation = {
  id: "chat-1",
  title: "Recovery and downtime",
  context_version: 1,
  patient_id: null,
  as_of: null,
  created_at: "2026-09-11T12:34:00Z",
  pinned: false,
  updated_at: "2026-09-11T12:34:00Z",
  title_origin: "auto",
  summary: null,
  summary_status: "idle",
  summary_error: null,
};
let chat: Conversation;
let deleted: boolean;
let failure: string;
let requests: { path: string; method: string; body: Record<string, unknown> }[];
const choose = vi.fn(),
  newChat = vi.fn(),
  removed = vi.fn(),
  close = vi.fn();
beforeEach(() => {
  chat = { ...original };
  deleted = false;
  failure = "";
  requests = [];
  choose.mockReset();
  newChat.mockReset();
  removed.mockReset();
  close.mockReset();
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const path = String(url).replace("/api/v1", ""),
        method = init?.method ?? "GET";
      const body = init?.body ? JSON.parse(String(init.body)) : {};
      requests.push({ path, method, body });
      if (failure && method !== "GET")
        return new Response(JSON.stringify({ error: { message: failure } }), {
          status: 409,
        });
      let value: unknown;
      if (path.startsWith("/conversations?")) {
        const query = new URLSearchParams(path.split("?")[1]).get("q");
        value = {
          items:
            query === "cancellation"
              ? [{ ...chat, id: "older-chat", title: "Policy notice windows" }]
              : query
                ? []
                : [chat],
        };
      } else if (path === "/conversations")
        value = { items: deleted ? [] : [chat] };
      else if (path === "/conversations/chat-1" && method === "PATCH") {
        chat = { ...chat, ...body };
        value = chat;
      } else if (path === "/conversations/chat-1" && method === "DELETE") {
        deleted = true;
        value = null;
      } else if (path === "/conversations/chat-1/share" && method === "POST")
        value = { url: "/share/fixture-public-token" };
      else if (path === "/conversations/chat-1/share" && method === "DELETE")
        value = null;
      else throw new Error(`Unexpected request ${method} ${path}`);
      return value === null
        ? new Response(null, { status: 204 })
        : new Response(JSON.stringify(value), {
            headers: { "Content-Type": "application/json" },
          });
    }),
  );
});
function setup() {
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0 },
      mutations: { retry: false },
    },
  });
  const content = (owner = "owner") => (
    <QueryClientProvider client={client}>
      <Sidebar
        owner={owner}
        open
        currentId="chat-1"
        busy={false}
        onChoose={choose}
        onClose={close}
        onNew={newChat}
        onDeleted={removed}
        onChanged={() => {}}
      />
    </QueryClientProvider>
  );
  const view = render(content());
  return { ...view, changeOwner: () => view.rerender(content("new-owner")) };
}
async function options(name = original.title) {
  await userEvent.click(
    await screen.findByRole("button", { name: `Options for ${name}` }),
  );
}
describe("Functional sidebar with HTTP doubles", () => {
  it("places primary actions above collapsible Recents and keeps timestamps", async () => {
    setup();
    await screen.findByRole("button", { name: /^Recovery and downtime/ });
    expect(document.querySelector("time")?.textContent).toMatch(/\d:\d{2}/);
    await userEvent.click(screen.getByRole("button", { name: "Recents" }));
    expect(
      screen.queryByRole("navigation", { name: "Conversation history" }),
    ).toBeNull();
    await userEvent.click(
      screen.getByRole("button", { name: "New chat", exact: true }),
    );
    expect(newChat).toHaveBeenCalledOnce();
    await userEvent.click(screen.getByRole("button", { name: "Recents" }));
    expect(
      screen.getByRole("navigation", { name: "Conversation history" }),
    ).toBeTruthy();
  });
  it("pins with a real patch and moves the chat into Pinned", async () => {
    setup();
    await userEvent.click(
      await screen.findByRole("button", { name: `Pin ${original.title}` }),
    );
    await screen.findByRole("region", { name: "Pinned chats" });
    expect(
      requests.find((request) => request.method === "PATCH")?.body,
    ).toEqual({ pinned: true });
    await userEvent.click(
      screen.getByRole("button", { name: `Unpin ${original.title}` }),
    );
    await waitFor(() =>
      expect(screen.queryByRole("region", { name: "Pinned chats" })).toBeNull(),
    );
  });
  it("supports keyboard options and explicit rename, with errors preserving the edit", async () => {
    setup();
    await options();
    expect(document.activeElement).toBe(
      screen.getByRole("menuitem", { name: "Share" }),
    );
    await userEvent.keyboard("{ArrowDown}{Enter}");
    const field = screen.getByRole("textbox", { name: "Chat name" });
    await userEvent.clear(field);
    await userEvent.type(field, "A clearer topic");
    failure = "This change could not be saved.";
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    await screen.findByText(failure);
    expect((field as HTMLInputElement).value).toBe("A clearer topic");
    failure = "";
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    await screen.findByRole("button", { name: /^A clearer topic/ });
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(chat.title).toBe("A clearer topic");
  });
  it("does not share on menu/dialog opening; explicit creation and revocation are separate", async () => {
    setup();
    await options();
    await userEvent.click(screen.getByRole("menuitem", { name: "Share" }));
    expect(
      requests.filter((request) => request.method === "POST"),
    ).toHaveLength(0);
    expect(screen.getByText(/Anyone with the link/)).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "Create link" }));
    const link = await screen.findByRole("textbox", { name: "Shared link" });
    expect((link as HTMLInputElement).value).toContain(
      "/share/fixture-public-token",
    );
    await userEvent.click(screen.getByRole("button", { name: "Remove link" }));
    await screen.findByRole("button", { name: "Create link" });
    expect(
      requests.some(
        (request) =>
          request.path.endsWith("/share") && request.method === "DELETE",
      ),
    ).toBe(true);
  });
  it("surfaces failed clipboard copying without claiming success", async () => {
    setup();
    await options();
    await userEvent.click(screen.getByRole("menuitem", { name: "Share" }));
    await userEvent.click(screen.getByRole("button", { name: "Create link" }));
    await screen.findByRole("textbox", { name: "Shared link" });
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText: vi.fn().mockRejectedValue(new Error("denied")) },
    });
    await userEvent.click(screen.getByRole("button", { name: "Copy link" }));
    await screen.findByText(
      "Copy is unavailable. Select and copy the link above.",
    );
    expect(screen.queryByText("Copied")).toBeNull();
  });
  it("requires deletion confirmation and keeps active-chat failure explicit", async () => {
    setup();
    await options();
    await userEvent.click(screen.getByRole("menuitem", { name: "Delete" }));
    expect(requests.some((request) => request.method === "DELETE")).toBe(false);
    expect(screen.getByRole("button", { name: "Keep chat" })).toBeTruthy();
    failure = "End voice before deleting this chat.";
    await userEvent.click(screen.getByRole("button", { name: "Delete chat" }));
    await screen.findByText(failure);
    expect(removed).not.toHaveBeenCalled();
    failure = "";
    await userEvent.click(screen.getByRole("button", { name: "Delete chat" }));
    await waitFor(() => expect(removed).toHaveBeenCalledWith("chat-1"));
    await screen.findByText("No conversations yet");
  });
  it("searches server content beyond loaded titles and opens results with the keyboard", async () => {
    setup();
    await userEvent.click(
      screen.getByRole("button", { name: "Search chats", exact: true }),
    );
    await userEvent.type(
      screen.getByRole("textbox", { name: "Search all chats" }),
      "cancellation",
    );
    await within(screen.getByRole("dialog")).findByText(
      "Policy notice windows",
    );
    expect(
      requests.some((request) => request.path.includes("q=cancellation")),
    ).toBe(true);
    screen.getByRole("textbox", { name: "Search all chats" }).focus();
    await userEvent.keyboard("{ArrowDown}{Enter}");
    expect(choose).toHaveBeenCalledWith("older-chat");
  });
  it("clears private dialogs on an ownership change", async () => {
    const view = setup();
    await options();
    await userEvent.click(screen.getByRole("menuitem", { name: "Share" }));
    await userEvent.click(screen.getByRole("button", { name: "Create link" }));
    await screen.findByRole("textbox", { name: "Shared link" });
    act(() => view.changeOwner());
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.queryByDisplayValue(/fixture-public-token/)).toBeNull();
  });
  it("closes search on the native dialog cancel event", async () => {
    setup();
    fireEvent.keyDown(window, { key: "k", metaKey: true });
    fireEvent(
      screen.getByRole("dialog"),
      new Event("cancel", { bubbles: true, cancelable: true }),
    );
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});

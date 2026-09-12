import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import {
  useInfiniteQuery,
  useMutation,
  useQueryClient,
} from "@tanstack/react-query";
import { api, patch, post, type Conversation, type ShareCreated } from "./api";
import { Dialog } from "./Dialog";
import { Icon } from "./Icon";
import { SignInPlaceholder } from "./SignInPlaceholder";

type ChatPage = { items: Conversation[]; next_offset?: number | null };
type Action = { kind: "rename" | "delete" | "share"; chat: Conversation };
type Menu = { chat: Conversation; anchor: HTMLButtonElement };
const message = (error: unknown) =>
  error instanceof Error ? error.message : "This change could not be saved.";
const timestamp = (chat: Conversation) => chat.updated_at ?? chat.created_at;
function ChatTime({ chat }: { chat: Conversation }) {
  return (
    <time dateTime={timestamp(chat)}>
      {new Date(timestamp(chat)).toLocaleString(undefined, {
        month: "short",
        day: "numeric",
        hour: "numeric",
        minute: "2-digit",
      })}
    </time>
  );
}

export function Sidebar({
  owner,
  open,
  currentId,
  busy,
  onClose,
  onChoose,
  onNew,
  onDeleted,
  onChanged,
}: {
  owner?: string;
  open: boolean;
  currentId: string | null;
  busy: boolean;
  onClose: () => void;
  onChoose: (id: string) => void;
  onNew: () => void;
  onDeleted: (id: string) => void;
  onChanged: () => void;
}) {
  const queryClient = useQueryClient();
  const [recents, setRecents] = useState(true);
  const [present, setPresent] = useState(open);
  useEffect(() => {
    if (open) {
      setPresent(true);
      return;
    }
    const timeout = window.setTimeout(() => setPresent(false), 180);
    return () => window.clearTimeout(timeout);
  }, [open]);
  const [searchOpen, setSearchOpen] = useState(false);
  const [search, setSearch] = useState("");
  const [debounced, setDebounced] = useState("");
  const [menu, setMenu] = useState<Menu | null>(null);
  const [action, setAction] = useState<Action | null>(null);
  const [name, setName] = useState("");
  const [shareUrl, setShareUrl] = useState("");
  const [copied, setCopied] = useState(false);
  const [copyError, setCopyError] = useState("");
  const sidebarClose = useRef<HTMLButtonElement>(null);
  const scroll = useRef<HTMLElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const closedOwner = useRef(owner);
  useEffect(() => {
    if (!open) setMenu(null);
  }, [open]);
  useEffect(() => {
    if (closedOwner.current !== owner) {
      closedOwner.current = owner;
      setAction(null);
      setMenu(null);
      setSearchOpen(false);
      setSearch("");
    }
  }, [owner]);
  const load = (offset: number, q = "") =>
    api<ChatPage>(
      `/conversations${offset || q ? `?${new URLSearchParams({ ...(offset ? { offset: String(offset) } : {}), ...(q ? { q } : {}) })}` : ""}`,
    );
  const next = (page: ChatPage, pages: ChatPage[]) =>
    page.next_offset !== undefined
      ? (page.next_offset ?? undefined)
      : page.items.length === 50
        ? pages.length * 50
        : undefined;
  const list = useInfiniteQuery({
    queryKey: [owner, "conversations"],
    queryFn: ({ pageParam }) => load(pageParam),
    initialPageParam: 0,
    getNextPageParam: next,
    enabled: !!owner,
    refetchInterval: (query) =>
      query.state.data?.pages.some((page) =>
        page.items.some((chat) =>
          ["pending", "generating"].includes(chat.summary_status ?? ""),
        ),
      )
        ? 1500
        : false,
  });
  const results = useInfiniteQuery({
    queryKey: [owner, "chat-search", debounced],
    queryFn: ({ pageParam }) => load(pageParam, debounced),
    initialPageParam: 0,
    getNextPageParam: next,
    enabled: !!owner && searchOpen,
    placeholderData: undefined,
  });
  useEffect(() => {
    const timer = setTimeout(() => setDebounced(search.trim()), 200);
    return () => clearTimeout(timer);
  }, [search]);
  useLayoutEffect(() => {
    if (open) sidebarClose.current?.focus({ preventScroll: true });
  }, [open]);
  useEffect(() => {
    const shortcut = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        if (document.querySelector("dialog[open]")) return;
        event.preventDefault();
        setSearchOpen(true);
      }
      if (
        event.key === "Escape" &&
        open &&
        !searchOpen &&
        !action &&
        !menu &&
        !document.querySelector("dialog[open]")
      )
        onClose();
    };
    window.addEventListener("keydown", shortcut);
    return () => window.removeEventListener("keydown", shortcut);
  }, [open, searchOpen, action, menu, onClose]);
  const update = async () => {
    if (closedOwner.current !== owner) return;
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: [owner, "conversations"] }),
      queryClient.invalidateQueries({ queryKey: [owner, "chat-search"] }),
    ]);
    onChanged();
  };
  const pin = useMutation({
    mutationFn: (chat: Conversation) =>
      patch<Conversation>(`/conversations/${chat.id}`, {
        pinned: !chat.pinned,
      }),
    onSuccess: update,
  });
  const mutation = useMutation({
    mutationFn: async (value: Action) => {
      if (value.kind === "rename") {
        await patch<Conversation>(`/conversations/${value.chat.id}`, {
          title: name.trim(),
        });
        return { kind: "rename" as const };
      }
      if (value.kind === "delete") {
        await api(`/conversations/${value.chat.id}`, { method: "DELETE" });
        return { kind: "delete" as const };
      }
      const shared = await post<ShareCreated>(
        `/conversations/${value.chat.id}/share`,
      );
      return { kind: "share" as const, url: shared.url };
    },
    onSuccess: async (value, submitted) => {
      if (closedOwner.current !== owner) return;
      if (value.kind === "share") {
        setShareUrl(new URL(value.url, window.location.origin).href);
      } else {
        if (submitted.kind === "delete") onDeleted(submitted.chat.id);
        setAction(null);
      }
      await update();
    },
  });
  const revoke = useMutation({
    mutationFn: (chat: Conversation) =>
      api(`/conversations/${chat.id}/share`, { method: "DELETE" }),
    onSuccess: () => {
      if (closedOwner.current !== owner) return;
      setShareUrl("");
      setCopied(false);
    },
  });
  const openAction = (kind: Action["kind"], chat: Conversation) => {
    menu?.anchor.focus({ preventScroll: true });
    setMenu(null);
    mutation.reset();
    revoke.reset();
    setCopyError("");
    setCopied(false);
    setShareUrl("");
    setName(chat.title);
    setAction({ kind, chat });
  };
  useLayoutEffect(() => {
    if (menu)
      menuRef.current?.querySelector<HTMLButtonElement>("button")?.focus();
  }, [menu]);
  useEffect(() => {
    if (!menu) return;
    const close = () => setMenu(null);
    const outside = (event: PointerEvent) => {
      if (
        event.target instanceof Node &&
        !menuRef.current?.contains(event.target) &&
        !menu.anchor.contains(event.target)
      )
        close();
    };
    document.addEventListener("pointerdown", outside);
    window.addEventListener("resize", close);
    scroll.current?.addEventListener("scroll", close);
    const element = scroll.current;
    return () => {
      document.removeEventListener("pointerdown", outside);
      window.removeEventListener("resize", close);
      element?.removeEventListener("scroll", close);
    };
  }, [menu]);
  const unique = (pages?: ChatPage[]) => [
    ...new Map(
      (pages?.flatMap((page) => page.items) ?? []).map((chat) => [
        chat.id,
        chat,
      ]),
    ).values(),
  ];
  const chats = unique(list.data?.pages);
  const found = unique(results.data?.pages);
  const pinned = chats.filter((chat) => chat.pinned);
  const recent = chats.filter((chat) => !chat.pinned);
  const row = (chat: Conversation) => (
    <div
      key={chat.id}
      className={`chat-entry ${currentId === chat.id ? "is-selected" : ""} ${menu?.chat.id === chat.id ? "has-menu" : ""}`}
    >
      <button
        className="chat-entry-open"
        disabled={busy}
        aria-current={currentId === chat.id ? "page" : undefined}
        onClick={() => onChoose(chat.id)}
      >
        <span className="chat-entry-title">
          {chat.title || "Untitled chat"}
        </span>
        <ChatTime chat={chat} />
      </button>
      <div className="chat-entry-actions">
        <button
          className={`quiet-icon ${chat.pinned ? "is-pinned" : ""}`}
          disabled={pin.isPending}
          title={chat.pinned ? "Unpin chat" : "Pin chat"}
          aria-label={`${chat.pinned ? "Unpin" : "Pin"} ${chat.title}`}
          aria-pressed={!!chat.pinned}
          onClick={() => pin.mutate(chat)}
        >
          <Icon name="pin" size={14} />
        </button>
        <button
          className="quiet-icon"
          title="Chat options"
          aria-label={`Options for ${chat.title}`}
          aria-haspopup="menu"
          aria-expanded={menu?.chat.id === chat.id}
          onClick={(event) =>
            setMenu(
              menu?.chat.id === chat.id
                ? null
                : { chat, anchor: event.currentTarget },
            )
          }
        >
          <Icon name="more" size={17} />
        </button>
      </div>
    </div>
  );
  const rect = menu?.anchor.getBoundingClientRect();
  return (
    <>
      {(open || present) && (
        <>
          <button
            className="sidebar-scrim"
            aria-label="Close sidebar backdrop"
            onClick={onClose}
            tabIndex={-1}
            hidden={!open}
          />
          <aside
            id="workspace-sidebar"
            className="chat-sidebar"
            aria-label="Sidebar"
            data-state={open ? "open" : "closed"}
            aria-hidden={!open}
            inert={!open}
          >
            <header className="sidebar-top">
              <div>
                <button
                  className="quiet-icon"
                  data-tooltip="Search chats"
                  aria-label="Search history"
                  onClick={() => setSearchOpen(true)}
                >
                  <Icon name="search" />
                </button>
                <button
                  ref={sidebarClose}
                  className="quiet-icon"
                  data-tooltip="Close sidebar"
                  aria-label="Close sidebar"
                  onClick={onClose}
                >
                  <Icon name="sidebar" />
                </button>
              </div>
            </header>
            <nav className="sidebar-primary" aria-label="Workspace">
              <button onClick={onNew} disabled={busy}>
                <Icon name="compose" />
                <span>New chat</span>
              </button>
              <button onClick={() => setSearchOpen(true)}>
                <Icon name="search" />
                <span>Search chats</span>
                <kbd aria-hidden="true">
                  {/Mac|iPhone|iPad/.test(navigator.platform) ? (
                    <Icon name="command" size={13} />
                  ) : (
                    <span>Ctrl</span>
                  )}
                  <span>K</span>
                </kbd>
              </button>
            </nav>
            <div className="sidebar-recents-heading">
              <button
                aria-expanded={recents}
                aria-controls="sidebar-recents"
                onClick={() => {
                  setMenu(null);
                  setRecents((value) => !value);
                }}
              >
                <span>Recents</span>
                <Icon name="chevron" size={12} />
              </button>
              <button
                className="quiet-icon"
                title="New chat"
                aria-label="New chat in Recents"
                disabled={busy}
                onClick={onNew}
              >
                <Icon name="compose" size={16} />
              </button>
            </div>
            <nav
              ref={scroll}
              id="sidebar-recents"
              className="sidebar-scroll"
              aria-label="Conversation history"
              hidden={!recents}
              onScroll={(event) => {
                const el = event.currentTarget;
                if (
                  el.scrollHeight - el.scrollTop - el.clientHeight < 120 &&
                  list.hasNextPage &&
                  !list.isFetching &&
                  !list.error
                )
                  void list.fetchNextPage();
              }}
            >
              {!owner && (
                <p className="sidebar-notice">Connect to see your chats.</p>
              )}
              {owner && list.isPending && (
                <p className="sidebar-notice" role="status">
                  Loading chats…
                </p>
              )}
              {list.error && (
                <div className="sidebar-notice" role="alert">
                  <strong>Can’t reach your chats</strong>
                  <p>{message(list.error)}</p>
                  <button
                    className="text-button"
                    disabled={list.isFetching}
                    onClick={() =>
                      void (list.isFetchNextPageError
                        ? list.fetchNextPage()
                        : list.refetch())
                    }
                  >
                    Retry history
                  </button>
                </div>
              )}
              {pin.error && (
                <div className="sidebar-notice" role="alert">
                  {message(pin.error)}
                  <button className="text-button" onClick={() => pin.reset()}>
                    Dismiss
                  </button>
                </div>
              )}
              {!!pinned.length && (
                <section className="pinned-chats" aria-label="Pinned chats">
                  <h3>Pinned</h3>
                  {pinned.map(row)}
                </section>
              )}
              {recent.map(row)}
              {list.isSuccess && !chats.length && (
                <div className="sidebar-empty">
                  <Icon name="chat" size={22} />
                  <p>No conversations yet</p>
                  <span>Your chats will appear here.</span>
                </div>
              )}
              {list.hasNextPage && (
                <button
                  className="history-more"
                  disabled={list.isFetching}
                  onClick={() => void list.fetchNextPage()}
                >
                  {list.isFetchingNextPage
                    ? "Loading…"
                    : "Load earlier conversations"}
                </button>
              )}
            </nav>
            <div className="sidebar-bottom">
              <SignInPlaceholder />
            </div>
          </aside>
        </>
      )}
      {menu &&
        rect &&
        createPortal(
          <div
            ref={menuRef}
            className="chat-context-menu"
            role="menu"
            aria-label="Chat options"
            style={{
              top: Math.min(rect.bottom + 6, window.innerHeight - 195),
              left: Math.max(
                8,
                Math.min(rect.right - 180, window.innerWidth - 188),
              ),
            }}
            onKeyDown={(event) => {
              const buttons = [
                ...event.currentTarget.querySelectorAll<HTMLButtonElement>(
                  "button",
                ),
              ];
              const index = buttons.indexOf(
                document.activeElement as HTMLButtonElement,
              );
              if (event.key === "Escape") {
                event.stopPropagation();
                setMenu(null);
                menu.anchor.focus();
              }
              if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
                event.preventDefault();
                buttons[
                  event.key === "Home"
                    ? 0
                    : event.key === "End"
                      ? buttons.length - 1
                      : (index +
                          (event.key === "ArrowDown" ? 1 : -1) +
                          buttons.length) %
                        buttons.length
                ]?.focus();
              }
              if (event.key === "Tab") setMenu(null);
            }}
          >
            <button
              role="menuitem"
              onClick={() => openAction("share", menu.chat)}
            >
              <Icon name="share" size={16} />
              Share
            </button>
            <button
              role="menuitem"
              onClick={() => openAction("rename", menu.chat)}
            >
              <Icon name="edit" size={16} />
              Rename
            </button>
            <button
              role="menuitem"
              onClick={() => {
                pin.mutate(menu.chat);
                setMenu(null);
                menu.anchor.focus();
              }}
            >
              <Icon name="pin" size={16} />
              {menu.chat.pinned ? "Unpin chat" : "Pin chat"}
            </button>
            <button
              role="menuitem"
              className="destructive"
              onClick={() => openAction("delete", menu.chat)}
            >
              <Icon name="trash" size={16} />
              Delete
            </button>
          </div>,
          document.body,
        )}
      {searchOpen && (
        <Dialog
          title="Search chats"
          className="chat-search-dialog"
          onClose={() => setSearchOpen(false)}
        >
          <label className="chat-search-input">
            <Icon name="search" />
            <input
              data-dialog-autofocus
              aria-label="Search all chats"
              placeholder="Search by topic or something you said…"
              value={search}
              maxLength={200}
              onChange={(event) => setSearch(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "ArrowDown") {
                  event.preventDefault();
                  event.currentTarget
                    .closest("dialog")
                    ?.querySelector<HTMLButtonElement>(".search-result")
                    ?.focus();
                }
              }}
            />
            {search && (
              <button
                className="quiet-icon"
                aria-label="Clear chat search"
                onClick={() => setSearch("")}
              >
                <Icon name="close" size={14} />
              </button>
            )}
          </label>
          <div
            className="chat-search-results"
            aria-busy={results.isFetching || search.trim() !== debounced}
            onKeyDown={(event) => {
              if (!["ArrowDown", "ArrowUp"].includes(event.key)) return;
              const buttons = [
                ...event.currentTarget.querySelectorAll<HTMLButtonElement>(
                  ".search-result",
                ),
              ];
              const index = buttons.indexOf(
                document.activeElement as HTMLButtonElement,
              );
              event.preventDefault();
              buttons[
                (index +
                  (event.key === "ArrowDown" ? 1 : -1) +
                  buttons.length) %
                  buttons.length
              ]?.focus();
            }}
          >
            <h3>{debounced ? "Results" : "Recent chats"}</h3>
            {!owner && <p role="status">Reconnect to search your chats.</p>}
            {owner && (results.isPending || search.trim() !== debounced) && (
              <p role="status">Searching…</p>
            )}
            {results.error && (
              <div role="alert">
                <p>{message(results.error)}</p>
                <button
                  className="text-button"
                  onClick={() => void results.refetch()}
                >
                  Retry search
                </button>
              </div>
            )}
            {search.trim() === debounced &&
              found.map((chat) => (
                <button
                  key={chat.id}
                  className="search-result"
                  disabled={busy}
                  onClick={() => {
                    setSearchOpen(false);
                    onChoose(chat.id);
                  }}
                >
                  <Icon name={chat.pinned ? "pin" : "chat"} size={17} />
                  <span>
                    <strong>{chat.title}</strong>
                    <ChatTime chat={chat} />
                  </span>
                  <span className="search-open-label">
                    Open <Icon name="chevron" size={12} />
                  </span>
                </button>
              ))}
            {results.isSuccess &&
              search.trim() === debounced &&
              !found.length && (
                <p className="search-empty">
                  {debounced
                    ? "No matching chats. Try another word."
                    : "Your conversations will appear here."}
                </p>
              )}
            {results.hasNextPage && (
              <button
                className="history-more"
                disabled={results.isFetching}
                onClick={() => void results.fetchNextPage()}
              >
                More results
              </button>
            )}
          </div>
        </Dialog>
      )}
      {action && (
        <Dialog
          busy={mutation.isPending || revoke.isPending}
          title={
            action.kind === "rename"
              ? "Rename chat"
              : action.kind === "delete"
                ? "Delete chat?"
                : "Share chat"
          }
          onClose={() => {
            if (!mutation.isPending && !revoke.isPending) setAction(null);
          }}
        >
          {action.kind === "rename" && (
            <form
              onSubmit={(event) => {
                event.preventDefault();
                if (name.trim()) mutation.mutate(action);
              }}
            >
              <label className="rename-field">
                Chat name
                <input
                  data-dialog-autofocus
                  value={name}
                  maxLength={120}
                  onChange={(event) => setName(event.target.value)}
                />
              </label>
              <p className="dialog-note">
                Your name won’t be replaced by an automatic title.
              </p>
              <div className="dialog-actions">
                <button
                  type="button"
                  disabled={mutation.isPending}
                  onClick={() => setAction(null)}
                >
                  Cancel
                </button>
                <button
                  className="primary-action"
                  disabled={!name.trim() || mutation.isPending}
                >
                  {mutation.isPending ? "Saving…" : "Save"}
                </button>
              </div>
            </form>
          )}
          {action.kind === "delete" && (
            <>
              <p>
                Delete <strong>{action.chat.title}</strong> and its saved
                messages? Shared links will stop working. This can’t be undone.
              </p>
              <div className="dialog-actions">
                <button
                  data-dialog-autofocus
                  disabled={mutation.isPending}
                  onClick={() => setAction(null)}
                >
                  Keep chat
                </button>
                <button
                  className="danger-action"
                  disabled={mutation.isPending}
                  onClick={() => mutation.mutate(action)}
                >
                  {mutation.isPending ? "Deleting…" : "Delete chat"}
                </button>
              </div>
            </>
          )}
          {action.kind === "share" && (
            <>
              <p>
                Create a read-only snapshot of this conversation. Anyone with
                the link can read it. A new link replaces the previous one.
              </p>
              <p className="dialog-note">
                Synthetic-data demo. New messages won’t be added to this
                snapshot.
                {["localhost", "127.0.0.1"].includes(
                  window.location.hostname,
                ) && " This local preview link only works on this Mac."}
              </p>
              {revoke.isSuccess && !shareUrl && (
                <p role="status" className="dialog-note">
                  Sharing is off.
                </p>
              )}
              {shareUrl ? (
                <>
                  <label className="share-link">
                    Shared link
                    <input
                      readOnly
                      value={shareUrl}
                      onFocus={(event) => event.target.select()}
                    />
                  </label>
                  <div className="dialog-actions">
                    <button
                      disabled={revoke.isPending}
                      onClick={() => revoke.mutate(action.chat)}
                    >
                      Remove link
                    </button>
                    <button
                      className="primary-action"
                      onClick={async () => {
                        try {
                          await navigator.clipboard.writeText(shareUrl);
                          setCopied(true);
                          setCopyError("");
                        } catch {
                          setCopyError(
                            "Copy is unavailable. Select and copy the link above.",
                          );
                        }
                      }}
                    >
                      {copied ? "Copied" : "Copy link"}
                    </button>
                  </div>
                </>
              ) : (
                <div className="dialog-actions">
                  <button
                    disabled={revoke.isPending || mutation.isPending}
                    onClick={() => revoke.mutate(action.chat)}
                  >
                    Remove existing link
                  </button>
                  <button
                    className="primary-action"
                    disabled={mutation.isPending || revoke.isPending}
                    onClick={() => {
                      revoke.reset();
                      mutation.mutate(action);
                    }}
                  >
                    {mutation.isPending ? "Creating link…" : "Create link"}
                  </button>
                </div>
              )}
            </>
          )}
          {(mutation.error || revoke.error || copyError) && (
            <p className="dialog-error" role="alert">
              {copyError || message(mutation.error ?? revoke.error)}
            </p>
          )}
        </Dialog>
      )}
    </>
  );
}

import type { InfiniteData, QueryClient } from "@tanstack/react-query";
import type { Conversation } from "./api";

type Page = { items: Conversation[]; next_offset?: number | null };

export function syncConversationLists(
  client: QueryClient,
  owner: string,
  conversation: Conversation,
) {
  const {
    id,
    title,
    pinned,
    updated_at,
    title_origin,
    summary,
    summary_status,
    summary_error,
  } = conversation;
  const update = (data: InfiniteData<Page> | undefined) =>
    data && {
      ...data,
      pages: data.pages.map((page) => ({
        ...page,
        items: page.items.map((item) =>
          item.id === id &&
          Date.parse(updated_at) >= Date.parse(item.updated_at)
            ? {
                ...item,
                title,
                pinned,
                updated_at,
                title_origin,
                summary,
                summary_status,
                summary_error,
              }
            : item,
        ),
      })),
    };
  client.setQueriesData<InfiniteData<Page>>(
    { queryKey: [owner, "conversations"] },
    update,
  );
  client.setQueriesData<InfiniteData<Page>>(
    { queryKey: [owner, "chat-search"] },
    update,
  );
}

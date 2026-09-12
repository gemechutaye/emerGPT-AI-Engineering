import { useQuery } from "@tanstack/react-query";
import { api, type SharedSnapshot } from "./api";
import { Icon } from "./Icon";
import { AssistantIdentity } from "./AssistantIdentity";
import { Brand } from "./Brand";
import { RecapText } from "./ConversationRecap";
import { AppearanceMenu } from "./AppearanceMenu";

export function SharedChat({ token }: { token: string }) {
  const shared = useQuery({
    queryKey: ["shared-chat", token],
    queryFn: () => api<SharedSnapshot>(`/shared/${encodeURIComponent(token)}`),
    retry: false,
  });
  return (
    <div className="shared-page">
      <header>
        <a href="/" aria-label="emer-GPT home" className="sidebar-brand">
          <Brand />
        </a>
        <span>
          <Icon name="link" size={14} />
          Shared conversation
        </span>
        <AppearanceMenu />
      </header>
      <main>
        <p className="shared-disclosure">
          Read-only snapshot · synthetic-data demo
        </p>
        {shared.isPending && <p role="status">Opening the conversation…</p>}
        {shared.error && (
          <div role="alert">
            <h1>This conversation isn’t available</h1>
            <p>{shared.error.message}</p>
            <button
              className="text-button"
              onClick={() => void shared.refetch()}
            >
              Try again
            </button>
          </div>
        )}
        {shared.data && (
          <>
            <h1>{shared.data.title}</h1>
            {shared.data.summary && (
              <section className="conversation-recap">
                <h2>Conversation recap</h2>
                <RecapText text={shared.data.summary} />
              </section>
            )}
            {shared.data.messages.map((entry, index) => (
              <article className={`shared-message ${entry.role}`} key={index}>
                <h2>{entry.role === "user" ? "You" : <AssistantIdentity />}</h2>
                <p>{entry.text}</p>
              </article>
            ))}
          </>
        )}
        <footer>
          This is a shared copy, not a live conversation. Not for clinical care.
        </footer>
      </main>
    </div>
  );
}

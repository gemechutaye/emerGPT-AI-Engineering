import { Brand } from "./Brand";

/** One product identity for typed answers, transcripts and shared copies. */
export function AssistantIdentity({
  active = false,
  compact = false,
}: {
  active?: boolean;
  compact?: boolean;
}) {
  return (
    <span
      className={`assistant-identity${active ? " is-working" : ""}${compact ? " is-compact" : ""}`}
      role="img"
      aria-label="emer-GPT"
    >
      <span className="assistant-brand" aria-hidden="true">
        <Brand />
      </span>
    </span>
  );
}

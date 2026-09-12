import { useId, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, type Answer, type Citation, type Source } from "./api";
import { Icon } from "./Icon";
import "./answer-sources.css";

type CitedSource = { key: string; citations: Citation[]; claims: string[] };

function groupSources(answer: Answer): CitedSource[] {
  const groups = new Map<string, CitedSource>();
  for (const statement of [...answer.statements, ...answer.next_steps]) {
    for (const citation of statement.citations ?? []) {
      const key = JSON.stringify([
        citation.doc_id,
        citation.index_id,
        citation.version,
      ]);
      const group = groups.get(key) ?? { key, citations: [], claims: [] };
      if (!group.claims.includes(statement.text))
        group.claims.push(statement.text);
      if (
        !group.citations.some(
          (item) =>
            item.start === citation.start &&
            item.end === citation.end &&
            item.quote === citation.quote &&
            item.source_sha256 === citation.source_sha256,
        )
      )
        group.citations.push(citation);
      groups.set(key, group);
    }
  }
  return [...groups.values()];
}

function SourcePassages({ group }: { group: CitedSource }) {
  const citation = group.citations[0];
  const source = useQuery({
    queryKey: [
      "answer-source",
      citation.doc_id,
      citation.index_id,
      citation.version,
    ],
    queryFn: ({ signal }) =>
      api<Source & { sha256: string }>(
        `/sources/${encodeURIComponent(citation.doc_id)}?index_id=${encodeURIComponent(citation.index_id)}`,
        { signal },
      ),
    retry: false,
    staleTime: Infinity,
  });
  // Server offsets count Unicode code points, rather than JavaScript UTF-16 units.
  const characters = Array.from(source.data?.text ?? "");
  const matches =
    !!source.data &&
    group.citations.every(
      (item) =>
        source.data!.doc_id === item.doc_id &&
        source.data!.index_id === item.index_id &&
        source.data!.version === item.version &&
        source.data!.sha256 === item.source_sha256 &&
        Number.isInteger(item.start) &&
        Number.isInteger(item.end) &&
        item.start >= 0 &&
        item.end > item.start &&
        item.end <= characters.length &&
        characters.slice(item.start, item.end).join("") === item.quote,
    );
  const ranges: { start: number; end: number }[] = [];
  if (matches) {
    for (const item of [...group.citations].sort((a, b) => a.start - b.start)) {
      const previous = ranges.at(-1);
      if (previous && item.start <= previous.end)
        previous.end = Math.max(previous.end, item.end);
      else ranges.push({ start: item.start, end: item.end });
    }
  }
  return (
    <>
      <div className="answer-source-provenance">
        <span>{citation.doc_id}</span>
        <span>Version {citation.version}</span>
        <span>Effective {citation.effective_date}</span>
      </div>
      <p className="answer-source-authority">{citation.authority}</p>
      {source.isPending && <p role="status">Opening the cited record…</p>}
      {(source.error || (source.data && !matches)) && (
        <div className="answer-source-error" role="alert">
          <p>
            {source.error
              ? "This source couldn’t be loaded. Your answer is still saved."
              : "The saved citation does not match this record. Its passage cannot be verified."}
          </p>
          <button type="button" onClick={() => void source.refetch()}>
            Try again
          </button>
        </div>
      )}
      {matches && (
        <div className="answer-source-original">
          <h3 className="answer-source-subtitle">Cited by these statements</h3>
          <ul className="answer-source-claims">
            {group.claims.map((claim) => (
              <li key={claim}>{claim}</li>
            ))}
          </ul>
          <h3 className="answer-source-subtitle">Original record</h3>
          <p className="answer-source-highlight-note">
            Passages cited by this answer are highlighted below.
          </p>
          <p className="answer-source-text">
            {ranges.map((range, index) => (
              <span key={range.start}>
                {characters
                  .slice(index ? ranges[index - 1].end : 0, range.start)
                  .join("")}
                <mark>{characters.slice(range.start, range.end).join("")}</mark>
              </span>
            ))}
            {characters.slice(ranges.at(-1)?.end ?? 0).join("")}
          </p>
        </div>
      )}
    </>
  );
}

function SourceRow({ group, index }: { group: CitedSource; index: number }) {
  const [open, setOpen] = useState(false);
  const readerId = useId();
  const trigger = useRef<HTMLButtonElement>(null);
  const citation = group.citations[0];
  const close = () => {
    setOpen(false);
    trigger.current?.focus({ preventScroll: true });
  };
  return (
    <li
      className={open ? "is-expanded" : undefined}
      onKeyDown={(event) => {
        if (event.key === "Escape" && open) {
          event.stopPropagation();
          close();
        }
      }}
    >
      <button
        ref={trigger}
        type="button"
        className="answer-source-button"
        aria-expanded={open}
        aria-controls={open ? readerId : undefined}
        aria-label={`Source ${index + 1}: ${citation.title}, ${citation.doc_id}, version ${citation.version}`}
        onClick={() => setOpen((value) => !value)}
      >
        <span className="answer-source-number" aria-hidden="true">
          {index + 1}
        </span>
        <span className="answer-source-label">
          <span>{citation.title}</span>
          <small>
            {citation.authority} · v{citation.version}
          </small>
        </span>
        <Icon name="chevron" size={13} />
      </button>
      {open && (
        <section
          id={readerId}
          aria-label={`Source reader: ${citation.title}`}
          className="answer-source-reader"
        >
          <header>
            <h3>{citation.title}</h3>
            <button
              className="quiet-icon"
              aria-label="Close source reader"
              onClick={close}
            >
              <Icon name="close" size={17} />
            </button>
          </header>
          <SourcePassages group={group} />
        </section>
      )}
    </li>
  );
}

export function AnswerSources({ answer }: { answer: Answer }) {
  const groups = groupSources(answer);
  const labelId = useId();
  if (!groups.length) return null;
  return (
    <section className="answer-sources" aria-labelledby={labelId}>
      <h4 id={labelId}>
        Sources <span>{groups.length}</span>
      </h4>
      <ol className="answer-source-list">
        {groups.map((group, index) => (
          <SourceRow key={group.key} group={group} index={index} />
        ))}
      </ol>
    </section>
  );
}

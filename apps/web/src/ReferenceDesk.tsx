import { useEffect, useId, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, type Answer, type Citation, type Source } from "./api";
import { Icon } from "./Icon";

export type SourceSelection = { id: string; index?: string; quote?: string };
export function ReferenceDesk({
  sources,
  pending,
  error,
  onRetry,
  selection,
  onSelect,
  onClear,
  answer,
  patient,
  onAsk,
  askDisabled,
  onLibrary,
}: {
  sources: Source[];
  pending: boolean;
  error: unknown;
  onRetry: () => void;
  selection: SourceSelection | null;
  onSelect: (source: SourceSelection) => void;
  onClear: () => void;
  answer?: Answer | null;
  patient: string;
  onAsk: (source: Source) => void;
  askDisabled?: boolean;
  onLibrary: () => void;
}) {
  const [tab, setTab] = useState<"library" | "evidence">("library");
  const [search, setSearch] = useState("");
  const [category, setCategory] = useState("");
  const heading = useRef<HTMLHeadingElement>(null);
  const controlId = useId();
  const source = useQuery({
    queryKey: ["source", selection?.id, selection?.index],
    queryFn: () =>
      api<Source>(
        `/sources/${encodeURIComponent(selection!.id)}${selection?.index ? `?index_id=${encodeURIComponent(selection.index)}` : ""}`,
      ),
    enabled: !!selection,
    staleTime: Infinity,
  });
  const citations = Array.from(
    new Map(
      [...(answer?.statements ?? []), ...(answer?.next_steps ?? [])]
        .flatMap((statement) => statement.citations ?? [])
        .map(
          (citation) =>
            [`${citation.doc_id}:${citation.quote}`, citation] as const,
        ),
    ).values(),
  );
  const categories = [...new Set(sources.map((record) => record.category))];
  const filtered = sources.filter(
    (record) =>
      (!category || record.category === category) &&
      `${record.doc_id} ${record.title}`
        .toLowerCase()
        .includes(search.toLowerCase()),
  );
  useEffect(() => {
    if (answer) setTab("evidence");
  }, [answer]);
  useEffect(() => {
    if (!selection) return;
    const desk = document.getElementById("reference-desk");
    if (desk) desk.scrollTop = 0;
    heading.current?.focus({ preventScroll: true });
    if (window.innerWidth < 1000) desk?.scrollIntoView({ block: "start" });
  }, [selection]);
  const selectCitation = (citation: Citation) =>
    onSelect({
      id: citation.doc_id,
      quote: citation.quote,
      index: answer?.index_id,
    });
  const text = source.data?.text ?? "";
  const start = selection?.quote ? text.indexOf(selection.quote) : -1;
  return (
    <aside
      className="reference-desk"
      id="reference-desk"
      aria-label="Reference desk"
    >
      <div className="desk-title">
        <Icon name="book" size={18} />
        <h2>Sources</h2>
        <span>{sources.length || "—"}</span>
      </div>
      {selection ? (
        <div className="desk-document">
          <button className="desk-back" onClick={onClear}>
            <Icon name="chevron" size={13} />
            Back to {tab === "evidence" ? "answer sources" : "the library"}
          </button>
          <span className="document-eyebrow">
            Original record {selection.index ? "· used in this answer" : ""}
          </span>
          <h3 ref={heading} tabIndex={-1}>
            {source.data?.title ?? selection.id}
          </h3>
          {source.isPending && (
            <p role="status" className="desk-message">
              Opening the original record…
            </p>
          )}
          {source.error && (
            <div role="alert" className="desk-message">
              <p>This source couldn’t be loaded.</p>
              <button onClick={() => void source.refetch()}>Try again</button>
            </div>
          )}
          {source.data && (
            <>
              <div className="desk-document-meta">
                <span>{source.data.doc_id}</span>
                <span>v{source.data.version}</span>
                <span>{source.data.effective_date}</span>
              </div>
              <p className="desk-authority">{source.data.authority}</p>
              {selection.quote && (
                <div className="selected-passage">
                  <Icon name="check" size={13} />
                  {start >= 0
                    ? "Cited passage highlighted below"
                    : "Read the original text below"}
                </div>
              )}
              <div className="desk-source-text">
                {start >= 0 ? (
                  <>
                    {text.slice(0, start)}
                    <mark className="evidence-highlight">
                      {text.slice(start, start + selection!.quote!.length)}
                    </mark>
                    {text.slice(start + selection!.quote!.length)}
                  </>
                ) : (
                  text
                )}
              </div>
              <button
                className="button secondary desk-ask"
                disabled={askDisabled}
                onClick={() => onAsk(source.data!)}
              >
                <Icon name="plus" size={14} />
                Ask about this record
              </button>
            </>
          )}
        </div>
      ) : (
        <>
          <div
            className="desk-tabs"
            role="tablist"
            aria-label="Reference view"
            onKeyDown={(event) => {
              if (
                !["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)
              )
                return;
              event.preventDefault();
              const next =
                event.key === "Home"
                  ? "library"
                  : event.key === "End"
                    ? "evidence"
                    : tab === "library"
                      ? "evidence"
                      : "library";
              setTab(next);
              document.getElementById(`${next}-tab`)?.focus();
            }}
          >
            <button
              role="tab"
              id="library-tab"
              aria-selected={tab === "library"}
              tabIndex={tab === "library" ? 0 : -1}
              aria-controls="desk-library"
              onClick={() => setTab("library")}
            >
              Library
            </button>
            <button
              role="tab"
              id="evidence-tab"
              aria-selected={tab === "evidence"}
              tabIndex={tab === "evidence" ? 0 : -1}
              aria-controls="desk-evidence"
              onClick={() => setTab("evidence")}
            >
              Answer sources <span>{citations.length}</span>
            </button>
          </div>
          {tab === "library" ? (
            <div
              role="tabpanel"
              id="desk-library"
              aria-labelledby="library-tab"
            >
              <p className="desk-intro">
                The records behind your answers. Open any source to read it in
                full.
              </p>
              <label className="desk-search">
                <Icon name="search" size={16} />
                <span className="sr-only">Find a source record</span>
                <input
                  placeholder="Find a patient, treatment, policy…"
                  value={search}
                  onChange={(event) => setSearch(event.target.value)}
                />
              </label>
              <div className="desk-filters">
                <label className="sr-only" htmlFor="record-kind">
                  Record type
                </label>
                <select
                  id="record-kind"
                  value={category}
                  onChange={(event) => setCategory(event.target.value)}
                >
                  <option value="">All record types</option>
                  {categories.map((value) => (
                    <option key={value} value={value}>
                      {value.replaceAll("_", " ")}
                    </option>
                  ))}
                </select>
                <span>{filtered.length} records</span>
              </div>
              {pending && (
                <div className="desk-loading" role="status">
                  <span />
                  <span />
                  <span />
                  <p>Loading the practice library…</p>
                </div>
              )}
              {error ? (
                <div className="desk-message" role="alert">
                  <p>The library couldn’t load.</p>
                  <button onClick={onRetry}>Try again</button>
                </div>
              ) : null}
              {!pending && !error && filtered.length === 0 && (
                <div className="desk-message">
                  <Icon name="search" size={22} />
                  <h3>
                    {search || category
                      ? "No matching records"
                      : "No source records available"}
                  </h3>
                  <p>
                    {search || category
                      ? "Try a patient ID or a broader term."
                      : "Reload the library to check its status."}
                  </p>
                  <button
                    onClick={() => {
                      setSearch("");
                      setCategory("");
                      if (!search && !category) onRetry();
                    }}
                  >
                    {search || category ? "Clear filters" : "Reload library"}
                  </button>
                </div>
              )}
              <div className="desk-records">
                {filtered.slice(0, 8).map((record) => (
                  <button
                    key={record.doc_id}
                    id={`${controlId}-record-${record.doc_id}`}
                    className={`desk-record ${patient === record.doc_id ? "context-record" : ""}`}
                    onClick={() => onSelect({ id: record.doc_id })}
                  >
                    <span
                      className={`record-glyph ${record.category === "synthetic_patient" ? "patient" : ""}`}
                    >
                      <Icon
                        name={
                          record.category === "synthetic_patient"
                            ? "patient"
                            : "file"
                        }
                        size={18}
                      />
                    </span>
                    <span>
                      <strong>{record.title}</strong>
                      <small>
                        {record.doc_id} <i /> v{record.version}
                        {patient === record.doc_id ? " · Selected context" : ""}
                      </small>
                    </span>
                    <Icon name="chevron" size={13} />
                  </button>
                ))}
              </div>
              {filtered.length > 8 && (
                <button className="desk-all" onClick={onLibrary}>
                  Browse all {sources.length} records
                  <Icon name="chevron" size={14} />
                </button>
              )}
              <div className="source-principle">
                <Icon name="check" size={15} />
                <p>
                  Original records, explicit gaps.
                  <br />
                  <span>Answers link back to what’s actually supplied.</span>
                </p>
              </div>
            </div>
          ) : (
            <div
              role="tabpanel"
              id="desk-evidence"
              aria-labelledby="evidence-tab"
            >
              <p className="desk-intro">
                Evidence from the latest answer. Each passage opens its
                original, versioned record.
              </p>
              {citations.length === 0 && (
                <div className="desk-message">
                  <Icon name="book" size={24} />
                  <h3>
                    {answer ? "No cited passages" : "Sources will appear here"}
                  </h3>
                  <p>
                    {answer
                      ? "This answer needs clarification or has no supported source statements."
                      : "Ask a question to bring the relevant evidence into view."}
                  </p>
                </div>
              )}
              <div className="desk-evidence-list">
                {citations.map((citation, i) => (
                  <button
                    key={`${citation.doc_id}-${i}`}
                    id={`${controlId}-citation-${i}`}
                    className="evidence-note"
                    onClick={() => selectCitation(citation)}
                  >
                    <span className="evidence-note-top">
                      <span className="evidence-number">{i + 1}</span>
                      <strong>{citation.title}</strong>
                      <Icon name="chevron" size={13} />
                    </span>
                    <span className="evidence-note-quote">
                      “{citation.quote}”
                    </span>
                    <span className="evidence-note-meta">
                      {citation.doc_id} · v{citation.version} ·{" "}
                      {citation.effective_date}
                    </span>
                  </button>
                ))}
              </div>
            </div>
          )}
        </>
      )}
    </aside>
  );
}

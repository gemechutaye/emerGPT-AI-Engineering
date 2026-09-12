import { useState, type CSSProperties } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, type Source } from "./api";
import { Dialog } from "./Dialog";
import { Icon } from "./Icon";

type SourceSummary = Omit<Source, "text">;
type Collection = { index_id: string; count: number; sources: SourceSummary[] };
const topics: Record<string, { name: string; color: string }> = {
  synthetic_patient: { name: "Patient records", color: "#d6aa83" },
  procedure: { name: "Procedure guides", color: "#b1a2d1" },
  operations: { name: "Practice operations", color: "#93b4ca" },
  policy: { name: "Cancellation policies", color: "#a0adda" },
  care: { name: "Before & after care", color: "#c99eaf" },
  business: { name: "Pricing & lead status", color: "#afb985" },
  it: { name: "IT & AI systems", color: "#93bba7" },
  governance: { name: "AI usage & answer quality", color: "#cab48f" },
  practice: { name: "Practice overview", color: "#b4aaa1" },
};
const topic = (category: string) =>
  topics[category] ?? { name: category, color: "#b4aaa1" };

export function InsideMark() {
  return (
    <svg
      viewBox="0 0 24 24"
      width="18"
      height="18"
      fill="none"
      aria-hidden="true"
    >
      <rect
        x="3"
        y="3"
        width="7"
        height="7"
        rx="2"
        stroke="currentColor"
        strokeWidth="1.6"
      />
      <rect x="14" y="3" width="7" height="7" rx="2" fill="currentColor" />
      <rect
        x="3"
        y="14"
        width="7"
        height="7"
        rx="2"
        stroke="currentColor"
        strokeWidth="1.6"
      />
      <rect
        x="14"
        y="14"
        width="7"
        height="7"
        rx="2"
        stroke="currentColor"
        strokeWidth="1.6"
      />
    </svg>
  );
}

function useRecord(id: string, index: string) {
  return useQuery({
    queryKey: ["inside-record", index, id],
    queryFn: ({ signal }) =>
      api<Source>(
        `/sources/${encodeURIComponent(id)}?index_id=${encodeURIComponent(index)}`,
        { signal },
      ),
    staleTime: Infinity,
    retry: false,
  });
}

function RecordDialog({
  source,
  index,
  onClose,
}: {
  source: SourceSummary;
  index: string;
  onClose: () => void;
}) {
  const record = useRecord(source.doc_id, index);
  return (
    <Dialog
      title={source.title}
      className="inside-record-dialog"
      onClose={onClose}
    >
      <div className="inside-record-meta">
        <span>{source.doc_id}</span>
        <span>Version {source.version}</span>
        <span>Effective {source.effective_date}</span>
      </div>
      <p className="inside-record-authority">{source.authority}</p>
      {record.isPending && <p role="status">Opening the original record…</p>}
      {record.error && (
        <div role="alert">
          <p>This record couldn’t be loaded.</p>
          <button
            className="inside-text-button"
            onClick={() => void record.refetch()}
          >
            Try again
          </button>
        </div>
      )}
      {record.data && <p className="inside-original">{record.data.text}</p>}
    </Dialog>
  );
}

function SourceButton({
  source,
  onOpen,
}: {
  source: SourceSummary;
  onOpen: (source: SourceSummary) => void;
}) {
  return (
    <button
      className="dataset-source"
      onClick={() => onOpen(source)}
      aria-label={`Read ${source.title}, ${source.doc_id}`}
    >
      <span>{source.title}</span>
      <small>{source.doc_id}</small>
      <Icon name="arrow-right" size={14} />
    </button>
  );
}

function RecordLoading({ record }: { record: ReturnType<typeof useRecord> }) {
  if (record.isPending)
    return (
      <p className="dataset-loading" role="status">
        Loading record…
      </p>
    );
  if (record.error)
    return (
      <p className="dataset-loading" role="alert">
        Couldn’t load this record.{" "}
        <button
          className="inside-text-button"
          onClick={() => void record.refetch()}
        >
          Try again
        </button>
      </p>
    );
  return null;
}

// Copy only fields present in the original record. No inferred patient facts.
function field(text: string, label: string) {
  return text
    .split("\n")
    .find((line) => line.startsWith(`${label}:`))
    ?.slice(label.length + 1)
    .trim();
}
function PatientRecord({
  source,
  index,
  onOpen,
}: {
  source: SourceSummary;
  index: string;
  onOpen: (source: SourceSummary) => void;
}) {
  const record = useRecord(source.doc_id, index);
  const text = record.data?.text ?? "";
  const age = field(text, "Age");
  const goal = field(text, "Primary goal");
  const context = field(text, "Relevant context");
  const status = field(text, "Current status");
  return (
    <article className="dataset-patient" aria-label={source.doc_id}>
      <header>
        <span className="dataset-patient-id">
          <Icon name="patient" size={16} />
          {source.doc_id}
        </span>
        {age && <span>Age {age}</span>}
      </header>
      <RecordLoading record={record} />
      {record.data && (
        <>
          {goal && <h3>{goal}</h3>}
          <dl>
            {context && (
              <div>
                <dt>Context</dt>
                <dd>{context}</dd>
              </div>
            )}
            {status && (
              <div>
                <dt>Current status</dt>
                <dd>{status}</dd>
              </div>
            )}
          </dl>
          {!goal && !context && !status && (
            <p className="dataset-raw-record">{text}</p>
          )}
        </>
      )}
      <button
        className="dataset-read"
        onClick={() => onOpen(source)}
        aria-label={`Read full record ${source.doc_id}`}
      >
        Full record <Icon name="arrow-right" size={14} />
      </button>
    </article>
  );
}

function PolicyRecord({
  source,
  index,
  onOpen,
}: {
  source: SourceSummary;
  index: string;
  onOpen: (source: SourceSummary) => void;
}) {
  const record = useRecord(source.doc_id, index);
  const paragraphs = record.data?.text.split(/\n\s*\n/) ?? [];
  const hours = record.data?.text.match(
    /(?:less than|at least) (\d+) hours/iu,
  )?.[1];
  return (
    <article className="dataset-policy" aria-label={source.title}>
      <header>
        <span>{source.authority}</span>
        <span>v{source.version}</span>
      </header>
      <RecordLoading record={record} />
      {record.data && (
        <>
          {hours && (
            <div className="dataset-policy-hours">
              {hours}
              <span>hours’ notice</span>
            </div>
          )}
          <p className="dataset-policy-date">{paragraphs[0]}</p>
          <blockquote>{paragraphs[1] ?? record.data.text}</blockquote>
        </>
      )}
      <button className="dataset-read" onClick={() => onOpen(source)}>
        {source.doc_id} · Full policy <Icon name="arrow-right" size={14} />
      </button>
    </article>
  );
}

export function InsideEmer({
  onBack,
  voiceActive = false,
  onEndVoice,
}: {
  onBack: () => void;
  voiceActive?: boolean;
  onEndVoice: () => void;
}) {
  const [selected, setSelected] = useState<{
    source: SourceSummary;
    index: string;
  } | null>(null);
  const collection = useQuery({
    queryKey: ["inside-corpus"],
    queryFn: ({ signal }) => api<Collection>("/corpus", { signal }),
    staleTime: 60_000,
    retry: false,
  });
  const openSource = (source: SourceSummary) => {
    if (collection.data)
      setSelected({ source, index: collection.data.index_id });
  };
  const sources = collection.data?.sources ?? [];
  const patients = sources.filter(
    (source) => source.category === "synthetic_patient",
  );
  const procedures = sources.filter(
    (source) => source.category === "procedure",
  );
  const policies = sources
    .filter((source) => source.category === "policy")
    .sort((a, b) => a.effective_date.localeCompare(b.effective_date));
  const categories = [
    ...new Set(sources.map((source) => source.category)),
  ].sort(
    (a, b) =>
      sources.filter((s) => s.category === b).length -
      sources.filter((s) => s.category === a).length,
  );
  const officeCategories = categories.filter(
    (category) =>
      !["synthetic_patient", "procedure", "policy"].includes(category),
  );
  return (
    <main
      className="inside-page"
      id="inside-content"
      tabIndex={-1}
      aria-label="EMER dataset"
    >
      <div className="dataset-page">
        {voiceActive && (
          <div className="inside-live-notice" role="status">
            <span>Voice session in progress.</span>
            <button onClick={onBack}>Return to voice</button>
            <button onClick={onEndVoice}>End voice</button>
          </div>
        )}
        <header className="dataset-intro">
          <span className="dataset-label">Synthetic training data</span>
          <h1>The EMER dataset</h1>
          <p>
            Patient records, treatment information and practice policies for the
            fictional EMER Aesthetic &amp; Dermatology Center.
          </p>
        </header>
        {collection.isPending && (
          <p className="dataset-loading" role="status">
            Loading the dataset…
          </p>
        )}
        {collection.error && (
          <div className="dataset-loading" role="alert">
            <p>
              {collection.data
                ? "The dataset couldn’t be refreshed. Showing the previously loaded records."
                : "The dataset couldn’t be loaded."}
            </p>
            <button
              className="inside-text-button"
              onClick={() => void collection.refetch()}
            >
              Try again
            </button>
          </div>
        )}
        {collection.data && (
          <>
            <div className="dataset-overview">
              <dl className="inside-numbers">
                <div>
                  <dt>Source records</dt>
                  <dd>{sources.length}</dd>
                </div>
                <div>
                  <dt>Patient records</dt>
                  <dd>{patients.length}</dd>
                </div>
                <div>
                  <dt>Procedure guides</dt>
                  <dd>{procedures.length}</dd>
                </div>
              </dl>
              <div className="dataset-composition" aria-hidden="true">
                {categories.map((category) => (
                  <span
                    key={category}
                    style={{
                      flex: sources.filter((s) => s.category === category)
                        .length,
                      background: topic(category).color,
                    }}
                  />
                ))}
              </div>
              <ul className="dataset-legend" aria-label="Records by category">
                {categories.map((category) => (
                  <li key={category}>
                    <i style={{ background: topic(category).color }} />
                    <span>{topic(category).name}</span>
                    <strong>
                      {sources.filter((s) => s.category === category).length}
                    </strong>
                  </li>
                ))}
              </ul>
            </div>
            {patients.length > 0 && (
              <section
                className="dataset-section"
                aria-labelledby="dataset-patients-title"
              >
                <div className="dataset-section-heading">
                  <h2 id="dataset-patients-title">
                    Patient records <span>{patients.length}</span>
                  </h2>
                  <p>
                    Each record includes an age, a primary goal, relevant
                    context and current status. The text below comes directly
                    from those records.
                  </p>
                </div>
                <div className="dataset-patients">
                  {patients.map((source) => (
                    <PatientRecord
                      key={source.doc_id}
                      source={source}
                      index={collection.data.index_id}
                      onOpen={openSource}
                    />
                  ))}
                </div>
              </section>
            )}
            {procedures.length > 0 && (
              <section
                className="dataset-section"
                aria-labelledby="dataset-procedures-title"
              >
                <div className="dataset-section-heading">
                  <h2 id="dataset-procedures-title">
                    Procedure guides <span>{procedures.length}</span>
                  </h2>
                  <p>
                    The treatments and consultations covered in the collection.
                    Open a title to read the guide.
                  </p>
                </div>
                <div className="dataset-procedures">
                  {procedures.map((source) => (
                    <SourceButton
                      key={source.doc_id}
                      source={source}
                      onOpen={openSource}
                    />
                  ))}
                </div>
              </section>
            )}
            {officeCategories.length > 0 && (
              <section
                className="dataset-section"
                aria-labelledby="dataset-practice-title"
              >
                <div className="dataset-section-heading">
                  <h2 id="dataset-practice-title">How the practice works</h2>
                  <p>
                    The source documents covering operations, care, business and
                    AI use.
                  </p>
                </div>
                <div className="dataset-document-groups">
                  {officeCategories.map((category) => (
                    <div
                      className="dataset-document-group"
                      key={category}
                      style={
                        {
                          "--category-color": topic(category).color,
                        } as CSSProperties
                      }
                    >
                      <h3>
                        <i />
                        {topic(category).name}
                      </h3>
                      <div>
                        {sources
                          .filter((s) => s.category === category)
                          .map((source) => (
                            <SourceButton
                              key={source.doc_id}
                              source={source}
                              onOpen={openSource}
                            />
                          ))}
                      </div>
                    </div>
                  ))}
                </div>
              </section>
            )}
            {policies.length > 0 && (
              <section
                className="dataset-section"
                aria-labelledby="dataset-policy-title"
              >
                <div className="dataset-section-heading">
                  <h2 id="dataset-policy-title">
                    Cancellation policy: {policies.length} versions
                  </h2>
                  <p>
                    Both versions are kept in the dataset. Their effective dates
                    matter when answering a question about a particular time.
                  </p>
                </div>
                <div className="dataset-policies">
                  {policies.map((source) => (
                    <PolicyRecord
                      key={source.doc_id}
                      source={source}
                      index={collection.data.index_id}
                      onOpen={openSource}
                    />
                  ))}
                </div>
              </section>
            )}
            {sources.length === 0 && (
              <p className="dataset-loading">
                No records are available in this dataset.
              </p>
            )}
            <footer className="dataset-footer">
              Fictional training data. No real patient information. Not for
              clinical care.
            </footer>
          </>
        )}
        {selected && (
          <RecordDialog
            source={selected.source}
            index={selected.index}
            onClose={() => setSelected(null)}
          />
        )}
      </div>
    </main>
  );
}

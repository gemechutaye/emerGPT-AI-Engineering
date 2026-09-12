import type { Run } from "./api";
import "./run-details.css";

function object(value: unknown): Record<string, unknown> | undefined {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : undefined;
}
function ids(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string")
    : [];
}
function number(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) && value >= 0
    ? value
    : undefined;
}
function score(value: unknown): string {
  return typeof value === "number" && Number.isFinite(value)
    ? value.toPrecision(5)
    : "—";
}
function text(value: unknown): string | undefined {
  return typeof value === "string" && value ? value : undefined;
}
function SourceList({ label, value }: { label: string; value: unknown }) {
  const sources = ids(value);
  if (!sources.length) return null;
  return (
    <p>
      <strong>{label}:</strong> {sources.join(" · ")}
    </p>
  );
}

export function RetrievalDetails({ run }: { run: Run }) {
  const diagnostics = run.answer?.diagnostics;
  const retrieval = object(diagnostics?.retrieval);
  const selections = Array.isArray(retrieval?.scoped_selection)
    ? retrieval.scoped_selection
        .map(object)
        .filter((item) => item !== undefined)
    : [];
  const selected = new Set(
    selections.flatMap((item) => ids(item.selected_ids)),
  );
  const raw = ids(retrieval?.raw_ranked_ids);
  const selectionOrder = ids(retrieval?.selection_ranked_ids);
  const contextOrder = selectionOrder.filter((id) => selected.has(id));
  const lexical = ids(retrieval?.lexical_ranked_ids);
  const semantic = ids(retrieval?.semantic_ranked_ids);
  const scores = object(retrieval?.scores);
  const mode = text(retrieval?.mode);
  const requestedMode = text(retrieval?.mode_requested);
  const topK = number(retrieval?.top_k);
  const latency = number(retrieval?.latency_ms);
  const selectedCount = number(retrieval?.selected_count);
  const corpusCount = number(retrieval?.corpus_count);
  const fusion = object(retrieval?.fusion);
  const lexicalWeight = number(fusion?.lexical_weight);
  const semanticWeight = number(fusion?.semantic_weight);
  const rank = (list: string[], id: string) => {
    const index = list.indexOf(id);
    return index < 0 ? "—" : index + 1;
  };
  return (
    <details className="retrieval-details">
      <summary>Retrieval and grounding</summary>
      <div className="retrieval-body">
        <p className="retrieval-note">
          Saved evidence for this answer. This reflects the index and settings
          used when it ran.
        </p>
        <dl className="retrieval-identity">
          <div>
            <dt>Run</dt>
            <dd>{run.id}</dd>
          </div>
          <div>
            <dt>Index</dt>
            <dd>{run.answer?.index_id ?? run.index_id}</dd>
          </div>
        </dl>
        {!retrieval ? (
          <p>Retrieval diagnostics were not recorded for this answer.</p>
        ) : (
          <>
            <dl className="activity-metrics">
              {mode && (
                <div>
                  <dt>Search mode</dt>
                  <dd>{mode}</dd>
                </div>
              )}
              {topK !== undefined && (
                <div>
                  <dt>Top k per scope</dt>
                  <dd>{topK}</dd>
                </div>
              )}
              {selectedCount !== undefined && (
                <div>
                  <dt>Context records</dt>
                  <dd>
                    {selectedCount}
                    {corpusCount !== undefined ? ` of ${corpusCount}` : ""}
                  </dd>
                </div>
              )}
              {latency !== undefined && (
                <div>
                  <dt>Search computation</dt>
                  <dd>
                    {latency.toLocaleString(undefined, {
                      maximumFractionDigits: 3,
                    })}{" "}
                    ms
                  </dd>
                </div>
              )}
            </dl>
            {requestedMode && mode && requestedMode !== mode && (
              <p>
                Requested {requestedMode}; used {mode}. The saved run records a
                retrieval downgrade.
              </p>
            )}
            {mode === "full" && (
              <p>
                Full context mode supplied every permitted record. This run does
                not demonstrate top-k selection.
              </p>
            )}
            {text(retrieval.embedding_model) && (
              <p>
                <strong>Embedding model:</strong>{" "}
                {text(retrieval.embedding_model)}
              </p>
            )}
            {fusion?.algorithm === "weighted_reciprocal_rank_fusion" &&
              lexicalWeight !== undefined &&
              semanticWeight !== undefined && (
                <p>
                  Rank fusion weights: lexical {lexicalWeight}, semantic{" "}
                  {semanticWeight}.
                </p>
              )}
            <SourceList label="Context source order" value={contextOrder} />
            <SourceList
              label="Explicit source matches"
              value={retrieval.explicit_source_ids}
            />
            <SourceList
              label="Exact title matches"
              value={retrieval.exact_title_source_ids}
            />
            {raw.length > 0 && (
              <details className="retrieval-ranking">
                <summary>Search ranking · {raw.length} records</summary>
                <p>
                  Raw search order precedes identity and applicability
                  filtering. Scores are relevance signals, not answer
                  confidence. A dash means that value was not recorded.
                </p>
                <div className="retrieval-table-wrap">
                  <table>
                    <caption>Saved raw ranking and context selection</caption>
                    <thead>
                      <tr>
                        <th>Rank</th>
                        <th>Source</th>
                        <th>Lexical rank</th>
                        <th>Semantic rank</th>
                        <th>Score</th>
                        <th>In context</th>
                      </tr>
                    </thead>
                    <tbody>
                      {raw.map((id, index) => (
                        <tr key={`${id}-${index}`}>
                          <td>{index + 1}</td>
                          <th scope="row">{id}</th>
                          <td>{rank(lexical, id)}</td>
                          <td>{rank(semantic, id)}</td>
                          <td>{score(scores?.[id])}</td>
                          <td>
                            {selections.length
                              ? selected.has(id)
                                ? "Yes"
                                : "No"
                              : "Not recorded"}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </details>
            )}
          </>
        )}
        {run.answer?.scopes.map((scope, index) => {
          const selection = selections.find(
            (item) => item.part_id === scope.part_id,
          );
          const permitted = new Set(ids(selection?.permitted_ranked_ids));
          const excluded =
            selection && raw.length
              ? raw.filter((id) => !permitted.has(id))
              : [];
          return (
            <details className="retrieval-scope" key={scope.part_id}>
              <summary>
                Scope {index + 1} · {scope.as_of}
              </summary>
              <p>{scope.question}</p>
              <p>
                <strong>Date basis:</strong>{" "}
                {scope.date_origin === "today"
                  ? "Run date"
                  : scope.date_origin === "context"
                    ? "Conversation context"
                    : "Question"}
              </p>
              <p>
                <strong>Patient scope:</strong>{" "}
                {scope.patient_ids.length
                  ? scope.patient_ids.join(" · ")
                  : scope.all_patients
                    ? "All patient records explicitly requested"
                    : scope.patient_discovery
                      ? "Source-based identity lookup"
                      : "General knowledge"}
              </p>
              <SourceList
                label="Unknown patient identifiers"
                value={scope.unknown_patient_ids}
              />
              <SourceList
                label="Top-k after identity filtering"
                value={selection?.ranked_selected_ids}
              />
              <SourceList
                label="Excluded by identity filtering"
                value={excluded}
              />
              <SourceList
                label="Patient expansion"
                value={selection?.patient_expansion_ids}
              />
              <SourceList
                label="Policy version expansion"
                value={selection?.policy_expansion_ids}
              />
              <SourceList
                label="Procedure reference expansion"
                value={selection?.procedure_expansion_ids}
              />
              {selection?.procedure_expansion_limit_exceeded === true && (
                <p>
                  Procedure references exceeded the bounded expansion limit.
                </p>
              )}
              <SourceList
                label="Allowed supporting sources"
                value={scope.allowed_source_ids}
              />
              <SourceList
                label="Explicit source inspection"
                value={scope.source_reference_ids}
              />
              <SourceList
                label="Applicable policies"
                value={scope.applicable_policy_ids}
              />
              <SourceList
                label="Inapplicable policies"
                value={scope.inapplicable_policy_ids}
              />
              <SourceList
                label="Conflicting policies"
                value={scope.conflicting_policy_ids}
              />
              {!!scope.policy_windows?.length && (
                <ul className="retrieval-windows">
                  {scope.policy_windows.map((window) => (
                    <li key={window.doc_id}>
                      {window.doc_id}: {window.valid_from}
                      {window.valid_through
                        ? ` through ${window.valid_through}`
                        : " onward"}{" "}
                      · {window.relation.replaceAll("_", " ")}
                    </li>
                  ))}
                </ul>
              )}
              {scope.warnings.map((warning, i) => (
                <p className="retrieval-note" key={i}>
                  {warning}
                </p>
              ))}
            </details>
          );
        })}
        <dl className="retrieval-identity">
          {text(diagnostics?.citation_validation) && (
            <div>
              <dt>Citation check</dt>
              <dd>
                {text(diagnostics?.citation_validation)?.replaceAll("_", " ")}
              </dd>
            </div>
          )}
          {text(diagnostics?.support_check) && (
            <div>
              <dt>Automated support check</dt>
              <dd>{text(diagnostics?.support_check)?.replaceAll("_", " ")}</dd>
            </div>
          )}
          {text(diagnostics?.prompt_id) && (
            <div>
              <dt>Answer prompt</dt>
              <dd>{text(diagnostics?.prompt_id)}</dd>
            </div>
          )}
          {text(diagnostics?.checker_prompt_id) && (
            <div>
              <dt>Checker prompt</dt>
              <dd>{text(diagnostics?.checker_prompt_id)}</dd>
            </div>
          )}
        </dl>
        {text(diagnostics?.support_check) && (
          <p className="retrieval-note">
            Automated checks can miss errors. Read the displayed source passages
            when reviewing a claim.
          </p>
        )}
      </div>
    </details>
  );
}

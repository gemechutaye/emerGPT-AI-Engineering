import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "./api";

type EvaluationReport = {
  status: string;
  message?: string;
  stage?: string;
  assessment_at?: string;
  reviewer_type?: string;
  independent_human_review?: boolean;
  coverage?: { requested_drafts: number; reviewed_drafts: number };
  models?: {
    model: string;
    requested_drafts: number;
    reviewed_drafts: number;
    provider_or_schema_failures: number;
    required_facts: { present: number; requested_total: number };
    unsupported: { drafts_with_critical_claims: number };
  }[];
  limitations?: string[];
  pending_gates?: string[];
};

export function EvaluationSummary() {
  const [open, setOpen] = useState(false);
  const report = useQuery({
    queryKey: ["evaluations"],
    queryFn: () => api<EvaluationReport>("/evaluations"),
    enabled: open,
    staleTime: 60000,
    retry: false,
  });
  const data = report.data;
  return (
    <details
      className="evaluation-summary"
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary>Measured quality</summary>
      {report.isLoading && <p role="status">Loading the evaluation report…</p>}
      {report.error && (
        <div role="alert">
          <p>The evaluation report could not be loaded.</p>
          <button className="button secondary" onClick={() => report.refetch()}>
            Try again
          </button>
        </div>
      )}
      {data && (
        <>
          <p className="evaluation-stage">
            {(data.stage ?? data.status).replaceAll("_", " ")}
          </p>
          {data.coverage ? (
            <p>
              <strong>
                {data.coverage.reviewed_drafts} of{" "}
                {data.coverage.requested_drafts} drafts reviewed.
              </strong>{" "}
              These development results do not establish final acceptance.
            </p>
          ) : (
            <p>{data.message ?? "Measured results are pending."}</p>
          )}
          {data.reviewer_type && (
            <p className="muted">
              Review:{" "}
              {data.reviewer_type === "author_subagent_review"
                ? "author-agent review"
                : data.reviewer_type.replaceAll("_", " ")}
              . Independent human review:{" "}
              {data.independent_human_review ? "yes" : "no"}.
            </p>
          )}
          {!!data.models?.length && (
            <div
              className="evaluation-table-scroll"
              role="region"
              aria-label="Model comparison"
              tabIndex={0}
            >
              <table>
                <caption>
                  Development model comparison. Facts use the full requested
                  denominator, including failed drafts.
                </caption>
                <thead>
                  <tr>
                    <th scope="col">Model</th>
                    <th scope="col">Required facts</th>
                    <th scope="col">Critical-claim drafts</th>
                    <th scope="col">Failed requests</th>
                  </tr>
                </thead>
                <tbody>
                  {data.models.map((model) => (
                    <tr key={model.model}>
                      <th scope="row">{model.model}</th>
                      <td>
                        {model.required_facts.present} /{" "}
                        {model.required_facts.requested_total}
                      </td>
                      <td>
                        {model.unsupported.drafts_with_critical_claims} /{" "}
                        {model.reviewed_drafts} reviewed
                      </td>
                      <td>
                        {model.provider_or_schema_failures} /{" "}
                        {model.requested_drafts}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {!!data.pending_gates?.length && (
            <>
              <h4>Checks still required</h4>
              <ul>
                {data.pending_gates.map((gate) => (
                  <li key={gate}>{gate}</li>
                ))}
              </ul>
            </>
          )}
          {!!data.limitations?.length && (
            <details className="evaluation-limits">
              <summary>How to read these results</summary>
              <ul>
                {data.limitations.map((limit) => (
                  <li key={limit}>{limit}</li>
                ))}
              </ul>
            </details>
          )}
          <a
            href="/api/v1/evaluations"
            target="_blank"
            rel="noopener noreferrer"
          >
            Open the full evaluation report ↗
          </a>
        </>
      )}
    </details>
  );
}

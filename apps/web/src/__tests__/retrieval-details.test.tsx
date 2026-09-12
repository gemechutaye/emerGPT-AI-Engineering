import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { RetrievalDetails } from "../RetrievalDetails";
import type { Run } from "../api";

function savedRun(): Run {
  return {
    id: "saved-run",
    conversation_id: "conversation",
    context_version: 2,
    index_id: "original-index",
    question: "Which cancellation rule applied?",
    status: "completed",
    metrics: {},
    created_at: "2026-09-12T10:00:00Z",
    completed_at: "2026-09-12T10:00:10Z",
    answer: {
      status: "answered",
      statements: [],
      next_steps: [],
      gaps: [],
      usage: [],
      index_id: "original-index",
      index_checksum: "original-checksum",
      scopes: [
        {
          part_id: "part-1",
          question: "Which cancellation rule applied?",
          patient_ids: [],
          unknown_patient_ids: [],
          as_of: "2026-06-18",
          date_origin: "question",
          all_patients: false,
          patient_discovery: false,
          warnings: [],
          allowed_source_ids: ["OPS-306-V1"],
          applicable_policy_ids: ["OPS-306-V1"],
          inapplicable_policy_ids: ["OPS-306-V2"],
          policy_windows: [
            {
              doc_id: "OPS-306-V2",
              family: "appointment-cancellation",
              valid_from: "2026-07-01",
              valid_through: null,
              relation: "not_yet_effective",
            },
          ],
        },
      ],
      diagnostics: {
        private_reasoning: "Do not render internal reasoning",
        citation_validation: "passed",
        support_check: "automated_pass",
        prompt_id: "answer-v14",
        checker_prompt_id: "check-v18",
        retrieval: {
          mode: "hybrid",
          mode_requested: "hybrid",
          top_k: 1,
          selected_count: 2,
          corpus_count: 35,
          latency_ms: 4.658,
          embedding_model: "openai/text-embedding-3-large",
          raw_ranked_ids: ["PT-006", "OPS-306-V1", "OPS-306-V2"],
          selection_ranked_ids: ["PT-006", "OPS-306-V1", "OPS-306-V2"],
          lexical_ranked_ids: ["OPS-306-V1"],
          semantic_ranked_ids: ["PT-006", "OPS-306-V2", "OPS-306-V1"],
          scores: { "OPS-306-V1": 0.06123 },
          fusion: {
            algorithm: "weighted_reciprocal_rank_fusion",
            lexical_weight: 1,
            semantic_weight: 3,
          },
          scoped_selection: [
            {
              part_id: "part-1",
              permitted_ranked_ids: ["OPS-306-V1", "OPS-306-V2"],
              ranked_selected_ids: ["OPS-306-V1"],
              selected_ids: ["OPS-306-V1", "OPS-306-V2"],
              patient_expansion_ids: [],
              policy_expansion_ids: ["OPS-306-V2"],
              procedure_expansion_ids: [],
              allowed_source_ids: ["OPS-306-V1"],
            },
          ],
        },
      },
    },
  };
}

describe("Saved retrieval details", () => {
  it("keeps engineering information opt-in and traces raw ranks separately from selected context", async () => {
    render(<RetrievalDetails run={savedRun()} />);
    const outer = screen
      .getByText("Retrieval and grounding")
      .closest("details")!;
    expect(outer.open).toBe(false);
    await userEvent.click(screen.getByText("Retrieval and grounding"));
    expect(outer.open).toBe(true);
    expect(
      screen.getByText("Context source order:").parentElement?.textContent,
    ).toBe("Context source order: OPS-306-V1 · OPS-306-V2");
    expect(screen.getByText("2 of 35")).toBeTruthy();
    expect(screen.getByText("4.658 ms")).toBeTruthy();
    expect(screen.getByText(/Rank fusion weights/).textContent).toContain(
      "lexical 1, semantic 3",
    );
    await userEvent.click(screen.getByText("Search ranking · 3 records"));
    const table = screen.getByRole("table", {
      name: "Saved raw ranking and context selection",
    });
    const rows = within(table).getAllByRole("row");
    expect(rows[1].textContent).toBe("1PT-006—1—No");
    expect(rows[2].textContent).toBe("2OPS-306-V1130.061230Yes");
    expect(screen.queryByText("Do not render internal reasoning")).toBeNull();
    expect(screen.getByText(/not answer confidence/)).toBeTruthy();
  });

  it("shows applicability, exclusions and expansion from the saved scope, not the current date", async () => {
    render(<RetrievalDetails run={savedRun()} />);
    await userEvent.click(screen.getByText("Retrieval and grounding"));
    await userEvent.click(screen.getByText("Scope 1 · 2026-06-18"));
    expect(
      screen.getByText("Excluded by identity filtering:").parentElement
        ?.textContent,
    ).toContain("PT-006");
    expect(
      screen.getByText("Policy version expansion:").parentElement?.textContent,
    ).toContain("OPS-306-V2");
    expect(
      screen.getByText("Allowed supporting sources:").parentElement
        ?.textContent,
    ).toBe("Allowed supporting sources: OPS-306-V1");
    expect(
      screen.getByText("OPS-306-V2: 2026-07-01 onward · not yet effective"),
    ).toBeTruthy();
    expect(screen.getByText("original-index")).toBeTruthy();
  });

  it("does not substitute current search claims or zero metrics when old runs lack diagnostics", async () => {
    const run = savedRun();
    run.answer!.diagnostics = {};
    run.answer!.scopes = [];
    render(<RetrievalDetails run={run} />);
    await userEvent.click(screen.getByText("Retrieval and grounding"));
    expect(
      screen.getByText(
        "Retrieval diagnostics were not recorded for this answer.",
      ),
    ).toBeTruthy();
    expect(screen.queryByText("Search mode")).toBeNull();
    expect(screen.queryByText("Search computation")).toBeNull();
    expect(screen.queryByText("Automated support check")).toBeNull();
  });

  it("labels a historical full-context run and an explicit retrieval downgrade truthfully", () => {
    const run = savedRun();
    run.answer!.diagnostics.retrieval = {
      mode: "full",
      mode_requested: "full",
      top_k: 8,
      selected_count: 27,
    };
    const view = render(<RetrievalDetails run={run} />);
    expect(
      screen.getByText(/does not demonstrate top-k selection/),
    ).toBeTruthy();
    run.answer!.diagnostics.retrieval = {
      mode: "lexical",
      mode_requested: "hybrid",
      latency_ms: -1,
      selected_count: NaN,
    };
    view.rerender(<RetrievalDetails run={run} />);
    expect(screen.getByText(/Requested hybrid; used lexical/)).toBeTruthy();
    expect(screen.queryByText("Search computation")).toBeNull();
    expect(screen.queryByText("Context records")).toBeNull();
  });

  it("handles a clarification with no answer or retrieval without inventing a completed search", () => {
    const run = savedRun();
    run.answer = null;
    render(<RetrievalDetails run={run} />);
    expect(
      screen.getByText(
        "Retrieval diagnostics were not recorded for this answer.",
      ),
    ).toBeTruthy();
    expect(screen.queryByText(/Scope 1/)).toBeNull();
  });
});

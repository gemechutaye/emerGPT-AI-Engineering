import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { AnswerSources } from "../AnswerSources";
import type { Answer, Citation, Source } from "../api";

// HTTP doubles verify citation provenance and navigation, not real corpus or layout acceptance.
const text =
  "📋 Record. Cost information was requested. No decision was recorded.";
const quote = "Cost information was requested.";
const start = Array.from(text.slice(0, text.indexOf(quote))).length;
const citation: Citation = {
  doc_id: "PT-006",
  title: "Synthetic Patient Record PT-006",
  version: "1.0",
  effective_date: "2026-09-01",
  authority: "Synthetic Patient Source",
  source_sha256: "source-hash",
  index_id: "historical-index",
  quote,
  start,
  end: start + quote.length,
};
const original: Source & { sha256: string } = {
  ...citation,
  category: "synthetic_patient",
  sha256: citation.source_sha256,
  text,
};
const answer: Answer = {
  status: "answered",
  index_id: "answer-index",
  index_checksum: "checksum",
  statements: [
    {
      part_id: "p1",
      text: "Cost information was requested.",
      citations: [citation],
    },
  ],
  gaps: [],
  next_steps: [],
  scopes: [],
  usage: [],
  diagnostics: {},
};
function show(value = answer) {
  return render(
    <QueryClientProvider
      client={
        new QueryClient({
          defaultOptions: { queries: { retry: false, gcTime: 0 } },
        })
      }
    >
      <AnswerSources answer={value} />
    </QueryClientProvider>,
  );
}
beforeEach(() => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response(JSON.stringify(original))),
  );
});

describe("End-of-answer sources", () => {
  it("shows only the claims that actually cite the opened source", async () => {
    show({
      ...answer,
      statements: [
        ...answer.statements,
        {
          part_id: "p2",
          text: "An unrelated claim.",
          citations: [{ ...citation, doc_id: "OTHER" }],
        },
      ],
      next_steps: [
        {
          part_id: "p1",
          text: "Clarify the cost request.",
          citations: [citation],
        },
      ],
    });
    await userEvent.click(screen.getByRole("button", { name: /^Source 1:/ }));
    const reader = screen.getByRole("region", { name: /^Source reader:/ });
    await within(reader).findByRole("heading", {
      name: "Cited by these statements",
    });
    expect(within(reader).getByText("Clarify the cost request.")).toBeTruthy();
    expect(within(reader).queryByText("An unrelated claim.")).toBeNull();
    expect(
      within(reader).getByText(
        "Passages cited by this answer are highlighted below.",
      ),
    ).toBeTruthy();
  });
  it("deduplicates cited records and next-step passages, while preserving distinct versions and indexes", async () => {
    const additional = {
      ...citation,
      quote: "No decision was recorded.",
      start: 42,
      end: 67,
    };
    show({
      ...answer,
      statements: [
        ...answer.statements,
        { part_id: "p2", text: "Repeated citation", citations: [citation] },
      ],
      next_steps: [
        {
          part_id: "p1",
          text: "Ask for the missing information.",
          citations: [
            additional,
            { ...citation, version: "2.0" },
            { ...citation, index_id: "other-index" },
          ],
        },
      ],
    });
    expect(screen.getAllByRole("button")).toHaveLength(3);
    expect(fetch).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: /^Source 1:/ }));
    await screen.findByRole("heading", { name: "Original record" });
    expect(document.querySelectorAll("mark")).toHaveLength(2);
  });

  it("merges overlapping verified passages without repeating the same excerpt", async () => {
    const longer = {
      ...citation,
      quote: Array.from(text).slice(start).join(""),
      end: Array.from(text).length,
    };
    show({
      ...answer,
      next_steps: [
        { part_id: "p1", text: "Check the record.", citations: [longer] },
      ],
    });
    await userEvent.click(screen.getByRole("button", { name: /^Source 1:/ }));
    await screen.findByRole("heading", { name: "Original record" });
    expect(document.querySelectorAll("mark")).toHaveLength(1);
    expect(document.querySelector("mark")?.textContent).toBe(longer.quote);
  });

  it("opens the original pinned source on demand and highlights Unicode code-point spans", async () => {
    show();
    expect(fetch).not.toHaveBeenCalled();
    const button = screen.getByRole("button", { name: /^Source 1:/ });
    await userEvent.click(button);
    await screen.findByRole("heading", { name: "Original record" });
    expect(fetch).toHaveBeenCalledWith(
      "/api/v1/sources/PT-006?index_id=historical-index",
      expect.anything(),
    );
    expect(screen.getByText("Version 1.0")).toBeTruthy();
    expect(screen.getByText("Effective 2026-09-01")).toBeTruthy();
    expect(document.querySelector("mark")?.textContent).toBe(quote);
    expect(document.querySelector("details, summary")).toBeNull();
    expect(document.querySelector("mark")?.textContent).toBe(quote);
    expect(document.querySelector(".answer-source-text")?.textContent).toBe(
      text,
    );
    await userEvent.click(
      screen.getByRole("button", { name: "Close source reader" }),
    );
    expect(
      screen.queryByRole("region", { name: /^Source reader:/ }),
    ).toBeNull();
    expect(button.getAttribute("aria-expanded")).toBe("false");
    expect(document.activeElement).toBe(button);
  });

  it.each([
    { index_id: "latest-index" },
    { version: "2.0" },
    { sha256: "changed-hash" },
    { text: text.replace("requested", "supplied") },
  ])(
    "does not present unverified excerpts when the returned record differs: %j",
    async (change) => {
      vi.mocked(fetch).mockResolvedValue(
        new Response(JSON.stringify({ ...original, ...change })),
      );
      show();
      await userEvent.click(screen.getByRole("button", { name: /^Source 1:/ }));
      await screen.findByRole("alert");
      expect(document.querySelector("mark")).toBeNull();
      expect(screen.queryByText("Original record")).toBeNull();
    },
  );

  it("lets a failed original-source request recover explicitly", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(new Response("{}", { status: 503 }));
    show();
    await userEvent.click(screen.getByRole("button", { name: /^Source 1:/ }));
    await screen.findByRole("alert");
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    await screen.findByRole("heading", { name: "Original record" });
    expect(screen.queryByRole("alert")).toBeNull();
    expect(fetch).toHaveBeenCalledTimes(2);
  });

  it("supports keyboard opening and Escape cancellation with focus restored", async () => {
    show();
    const button = screen.getByRole("button", { name: /^Source 1:/ });
    button.focus();
    await userEvent.keyboard("{Enter}");
    expect(
      screen.getByRole("region", { name: /^Source reader:/ }),
    ).toBeTruthy();
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(button.getAttribute("aria-expanded")).toBe("true");
    await userEvent.keyboard("{Escape}");
    expect(
      screen.queryByRole("region", { name: /^Source reader:/ }),
    ).toBeNull();
    expect(document.activeElement).toBe(button);
  });

  it("opens complete records directly inside their own rows without closing other sources", async () => {
    const second = {
      ...citation,
      doc_id: "OPS-306-V2",
      title: "Cancellation policy",
    };
    vi.mocked(fetch).mockImplementation(
      async (url) =>
        new Response(
          JSON.stringify(
            String(url).includes(second.doc_id)
              ? { ...original, ...second, text, sha256: second.source_sha256 }
              : original,
          ),
        ),
    );
    show({
      ...answer,
      statements: [{ ...answer.statements[0], citations: [citation, second] }],
    });
    const firstButton = screen.getByRole("button", { name: /^Source 1:/ });
    const secondButton = screen.getByRole("button", { name: /^Source 2:/ });
    await userEvent.click(firstButton);
    const firstReader = await screen.findByRole("region", {
      name: `Source reader: ${citation.title}`,
    });
    await within(firstReader).findByRole("heading", {
      name: "Original record",
    });
    expect(firstReader.parentElement).toBe(firstButton.parentElement);
    expect(firstButton.nextElementSibling).toBe(firstReader);
    expect(firstReader.parentElement?.nextElementSibling).toBe(
      secondButton.parentElement,
    );
    expect(firstReader.querySelector(".answer-source-text")?.textContent).toBe(
      text,
    );
    expect(firstReader.querySelector("details, summary")).toBeNull();
    await userEvent.click(secondButton);
    const secondReader = await screen.findByRole("region", {
      name: `Source reader: ${second.title}`,
    });
    await within(secondReader).findByRole("heading", {
      name: "Original record",
    });
    expect(firstButton.getAttribute("aria-expanded")).toBe("true");
    expect(secondReader.parentElement).toBe(secondButton.parentElement);
    expect(firstButton.getAttribute("aria-controls")).not.toBe(
      secondButton.getAttribute("aria-controls"),
    );
    await userEvent.click(
      within(firstReader).getByRole("button", { name: "Close source reader" }),
    );
    expect(document.activeElement).toBe(firstButton);
    expect(secondButton.getAttribute("aria-expanded")).toBe("true");
  });

  it("creates no fabricated source controls for answers without citations", () => {
    show({
      ...answer,
      status: "clarification",
      statements: [],
      gaps: [{ part_id: "p1", text: "Which patient?" }],
    });
    expect(screen.queryByRole("region")).toBeNull();
    expect(screen.queryByRole("button")).toBeNull();
    expect(fetch).not.toHaveBeenCalled();
  });
});

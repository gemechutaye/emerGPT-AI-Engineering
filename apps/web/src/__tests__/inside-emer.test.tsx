import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, expect, it, vi } from "vitest";
import { InsideEmer } from "../InsideEmer";
import type { Source } from "../api";

const records: Source[] = [
  {
    doc_id: "OPS-306-V1",
    title: "Earlier cancellation policy",
    category: "policy",
    version: "1.0",
    effective_date: "2026-01-01",
    authority: "Superseded Policy",
    text: "Earlier policy.\n\nRoutine appointments canceled with less than 24 hours' notice may be subject to a cancellation fee unless waived by management.",
  },
  {
    doc_id: "OPS-306-V2",
    title: "Current cancellation policy",
    category: "policy",
    version: "2.0",
    effective_date: "2026-07-01",
    authority: "Current Policy",
    text: "Current policy.\n\nRoutine appointments should be canceled or rescheduled at least 48 hours in advance when possible.",
  },
  {
    doc_id: "PT-005",
    title: "Synthetic Patient Record PT-005",
    category: "synthetic_patient",
    version: "1.0",
    effective_date: "2026-09-01",
    authority: "Synthetic Patient Source",
    text: "SYNTHETIC TRAINING RECORD - NOT A REAL PATIENT.\nAge: 47\nPrimary goal: lower-face volume and contour concerns\nRelevant context: Prior filler history is uncertain.\nCurrent status: Consultation incomplete pending prior product/treatment history. No injectable plan finalized.",
  },
  {
    doc_id: "PT-006",
    title: "Synthetic Patient Record PT-006",
    category: "synthetic_patient",
    version: "1.0",
    effective_date: "2026-09-01",
    authority: "Synthetic Patient Source",
    text: "SYNTHETIC TRAINING RECORD - NOT A REAL PATIENT.\nAge: 38\nPrimary goal: skin tightening and texture\nRelevant context: No prior RF microneedling.\nCurrent status: Patient requested cost information and will decide later.",
  },
  {
    doc_id: "PROC-102",
    title: "RF Microneedling - General Overview",
    category: "procedure",
    version: "1.0",
    effective_date: "2026-09-01",
    authority: "Procedure Education",
    text: "This is the original procedure guide.",
  },
  {
    doc_id: "IT-401",
    title: "AI / IT Incident Triage",
    category: "it",
    version: "1.0",
    effective_date: "2026-09-01",
    authority: "Internal Operations",
    text: "This is the original triage guide.",
  },
];
function mount() {
  const onBack = vi.fn(),
    onEndVoice = vi.fn();
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <InsideEmer onBack={onBack} onEndVoice={onEndVoice} />
    </QueryClientProvider>,
  );
  return { onBack, onEndVoice, client };
}
function setup(data = records) {
  const fetcher = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url === "/api/v1/corpus")
      return Response.json({
        index_id: "pinned/index",
        count: data.length,
        sources: data.map(({ text, ...record }) => record),
      });
    const record = data.find(
      (r) => url === `/api/v1/sources/${r.doc_id}?index_id=pinned%2Findex`,
    );
    return record
      ? Response.json(record)
      : Response.json({ detail: "Not found" }, { status: 404 });
  });
  vi.stubGlobal("fetch", fetcher);
  return { ...mount(), fetcher };
}
describe("Single-page dataset", () => {
  it("shows all sections together with computed counts and no chapter controls", async () => {
    const { fetcher } = setup();
    await screen.findByRole("heading", { name: /Patient records/ });
    expect(document.querySelector(".inside-numbers")?.textContent).toBe(
      "Source records6Patient records2Procedure guides1",
    );
    expect(
      screen.getByRole("heading", { name: /Procedure guides/ }),
    ).toBeTruthy();
    expect(
      screen.getByRole("heading", { name: "How the practice works" }),
    ).toBeTruthy();
    expect(
      screen.getByRole("heading", { name: "Cancellation policy: 2 versions" }),
    ).toBeTruthy();
    expect(screen.queryByRole("navigation")).toBeNull();
    expect(
      screen.queryByRole("button", {
        name: /Next chapter|Previous chapter|Back to chat|Try in chat/,
      }),
    ).toBeNull();
    expect(screen.queryByText("A guided look")).toBeNull();
    expect(
      fetcher.mock.calls.some((call) => String(call[0]).includes("/runs")),
    ).toBe(false);
  });
  it("presents original patient fields and both policy qualifications without a selection", async () => {
    setup();
    await screen.findByText("Prior filler history is uncertain.");
    expect(
      screen.getByText(
        "Patient requested cost information and will decide later.",
      ),
    ).toBeTruthy();
    await screen.findByText(/Routine appointments canceled/);
    await screen.findByText(/Routine appointments should be canceled/);
    expect(
      [...document.querySelectorAll(".dataset-policy-hours")].map(
        (e) => e.textContent,
      ),
    ).toEqual(["24hours’ notice", "48hours’ notice"]);
    expect(
      screen.getByText(/may be subject to a cancellation fee unless waived/),
    ).toBeTruthy();
    expect(screen.getByText(/in advance when possible/)).toBeTruthy();
  });
  it("opens original full records from the pinned index and restores focus", async () => {
    const { fetcher } = setup();
    const trigger = await screen.findByRole("button", {
      name: "Read full record PT-005",
    });
    await userEvent.click(trigger);
    const dialog = screen.getByRole("dialog", {
      name: "Synthetic Patient Record PT-005",
    });
    expect(
      (await within(dialog).findByText(/SYNTHETIC TRAINING RECORD/))
        .textContent,
    ).toBe(records[2].text);
    expect(fetcher.mock.calls.map((call) => String(call[0]))).toContain(
      "/api/v1/sources/PT-005?index_id=pinned%2Findex",
    );
    await userEvent.click(
      within(dialog).getByRole("button", {
        name: "Close synthetic patient record pt-005",
      }),
    );
    expect(document.activeElement).toBe(trigger);
  });
  it("renders changed fields as supplied rather than retaining an old interpretation", async () => {
    setup(
      records.map((r) =>
        r.doc_id === "PT-005"
          ? {
              ...r,
              text: "Age: 50\nPrimary goal: updated goal\nCurrent status: Updated source statement.",
            }
          : r,
      ),
    );
    await screen.findByText("Updated source statement.");
    expect(screen.getByText("Age 50")).toBeTruthy();
    expect(screen.queryByText("Prior filler history is uncertain.")).toBeNull();
  });
  it("offers retry on collection failure instead of fabricated data", async () => {
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValueOnce(
          Response.json({ detail: "Unavailable" }, { status: 503 }),
        )
        .mockResolvedValue(
          Response.json({ index_id: "i", count: 0, sources: [] }),
        ),
    );
    mount();
    await screen.findByRole("alert");
    expect(document.querySelector(".inside-numbers")).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    await screen.findByText("No records are available in this dataset.");
    expect(document.querySelector(".inside-numbers")?.textContent).toBe(
      "Source records0Patient records0Procedure guides0",
    );
  });
  it("retains return and End controls only when voice is active", () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => new Promise(() => undefined)),
    );
    const end = vi.fn(),
      back = vi.fn();
    render(
      <QueryClientProvider client={new QueryClient()}>
        <InsideEmer voiceActive onBack={back} onEndVoice={end} />
      </QueryClientProvider>,
    );
    fireEvent.click(screen.getByRole("button", { name: "Return to voice" }));
    fireEvent.click(screen.getByRole("button", { name: "End voice" }));
    expect(back).toHaveBeenCalledOnce();
    expect(end).toHaveBeenCalledOnce();
  });
  it("pins an open record's body and metadata when the active index changes", async () => {
    const { client, fetcher } = setup();
    await userEvent.click(
      await screen.findByRole("button", { name: "Read full record PT-005" }),
    );
    const dialog = screen.getByRole("dialog", { name: records[2].title });
    expect(
      (await within(dialog).findByText(/SYNTHETIC TRAINING RECORD/))
        .textContent,
    ).toBe(records[2].text);
    expect(within(dialog).getByText("Version 1.0")).toBeTruthy();
    expect(within(dialog).getByText("Effective 2026-09-01")).toBeTruthy();
    expect(within(dialog).getByText("Synthetic Patient Source")).toBeTruthy();
    act(() =>
      client.setQueryData(["inside-corpus"], {
        index_id: "replacement-index",
        count: 1,
        sources: [
          {
            ...records[2],
            version: "2.0",
            title: "Updated patient source",
            effective_date: "2026-10-01",
          },
        ],
      }),
    );
    await waitFor(() =>
      expect(document.querySelector(".inside-numbers")?.textContent).toBe(
        "Source records1Patient records1Procedure guides0",
      ),
    );
    expect(
      within(dialog).getByText(/SYNTHETIC TRAINING RECORD/).textContent,
    ).toBe(records[2].text);
    expect(within(dialog).getByText("Version 1.0")).toBeTruthy();
    expect(within(dialog).queryByText("Version 2.0")).toBeNull();
    // The replacement catalog's card may fetch its own source. The open dialog
    // must continue to use the original cached body and metadata together.
    expect(
      fetcher.mock.calls.some(([url]) =>
        String(url).includes("index_id=pinned%2Findex"),
      ),
    ).toBe(true);
  });

  it("retries an unavailable original source against the same pinned index", async () => {
    const record = records[4];
    let failed = true;
    const fetcher = vi.fn(async (input: RequestInfo | URL) => {
      if (String(input) === "/api/v1/corpus")
        return Response.json({
          index_id: "pinned/index",
          count: 1,
          sources: [{ ...record, text: undefined }],
        });
      return failed
        ? Response.json({ detail: "Unavailable" }, { status: 503 })
        : Response.json(record);
    });
    vi.stubGlobal("fetch", fetcher);
    mount();
    await userEvent.click(
      await screen.findByRole("button", {
        name: `Read ${record.title}, ${record.doc_id}`,
      }),
    );
    const dialog = screen.getByRole("dialog", { name: record.title });
    await within(dialog).findByRole("alert");
    expect(within(dialog).queryByText(record.text)).toBeNull();
    failed = false;
    await userEvent.click(
      within(dialog).getByRole("button", { name: "Try again" }),
    );
    expect(await within(dialog).findByText(record.text)).toBeTruthy();
    expect(within(dialog).queryByRole("alert")).toBeNull();
    const sourceCalls = fetcher.mock.calls.filter(([url]) =>
      String(url).includes("/sources/"),
    );
    expect(sourceCalls.map(([url]) => String(url))).toEqual([
      "/api/v1/sources/PROC-102?index_id=pinned%2Findex",
      "/api/v1/sources/PROC-102?index_id=pinned%2Findex",
    ]);
  });

  it("retains exact policy modality and version metadata in the full source view", async () => {
    setup();
    await userEvent.click(
      await screen.findByRole("button", { name: /OPS-306-V2 · Full policy/ }),
    );
    const dialog = screen.getByRole("dialog", {
      name: "Current cancellation policy",
    });
    expect(
      (
        await within(dialog).findByText(
          /Routine appointments should be canceled/,
        )
      ).textContent,
    ).toBe(records[1].text);
    expect(within(dialog).getByText("Version 2.0")).toBeTruthy();
    expect(within(dialog).getByText("Effective 2026-07-01")).toBeTruthy();
    expect(within(dialog).getByText("Current Policy")).toBeTruthy();
  });
  it("labels a failed catalog refresh while retaining the previously loaded records", async () => {
    const { client, fetcher } = setup();
    await screen.findByRole("heading", { name: /Patient records/ });
    await screen.findByText("Prior filler history is uncertain.");
    fetcher.mockImplementationOnce(async () =>
      Response.json({ detail: "Unavailable" }, { status: 503 }),
    );
    await act(async () => {
      await client.invalidateQueries({ queryKey: ["inside-corpus"] });
    });
    expect(
      await screen.findByText(
        "The dataset couldn’t be refreshed. Showing the previously loaded records.",
      ),
    ).toBeTruthy();
    expect(document.querySelector(".inside-numbers")?.textContent).toBe(
      "Source records6Patient records2Procedure guides1",
    );
    expect(screen.getByText("Prior filler history is uncertain.")).toBeTruthy();
  });
});

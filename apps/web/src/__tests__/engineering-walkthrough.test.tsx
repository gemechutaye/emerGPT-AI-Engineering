import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { EngineeringWalkthrough } from "../EngineeringWalkthrough";

describe("engineering presentation", () => {
  it("supports topic selection and bounded previous/next navigation", async () => {
    const user = userEvent.setup();
    render(<EngineeringWalkthrough />);
    expect(
      screen.getByRole("button", { name: "Previous slide" }),
    ).toHaveProperty("disabled", true);
    await user.click(screen.getByRole("button", { name: "Next slide" }));
    expect(
      screen.getByRole("heading", {
        name: "Retrieve evidence first. Generate from that evidence.",
      }),
    ).toBeTruthy();
    await user.click(
      screen.getByRole("button", { name: /07\s*Next priorities/ }),
    );
    expect(screen.getByRole("button", { name: "Next slide" })).toHaveProperty(
      "disabled",
      true,
    );
    await user.click(screen.getByRole("button", { name: "Previous slide" }));
    expect(
      screen.getByRole("heading", {
        name: "Show the answer. Then show why it can be trusted.",
      }),
    ).toBeTruthy();
  });

  it("copies a selected demo question without dispatching a request", async () => {
    const user = userEvent.setup();
    const clipboard = vi.spyOn(navigator.clipboard, "writeText");
    const fetchSpy = vi.spyOn(globalThis, "fetch");
    render(<EngineeringWalkthrough />);
    const question = "What is the practice’s emergency telephone number?";
    await user.click(screen.getByText(question));
    await user.click(
      within(screen.getByText(question).closest("details")!).getByRole(
        "button",
        { name: "Copy question" },
      ),
    );
    expect(clipboard).toHaveBeenCalledWith(question);
    expect(
      screen.getByText("Question 15 copied. Paste it into chat when ready."),
    ).toBeTruthy();
    expect(fetchSpy).not.toHaveBeenCalled();
    clipboard.mockRestore();
    fetchSpy.mockRestore();
  });
});

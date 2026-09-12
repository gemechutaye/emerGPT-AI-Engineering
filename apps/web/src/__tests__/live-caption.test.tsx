import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { LiveCaption, captionPage } from "../LiveCaption";
import { quietAudio } from "../liveAudio";
import type { TranscriptTurn } from "../liveTranscript";

const input: TranscriptTurn = {
  id: "input",
  speaker: "input",
  text: "What is the cancellation policy?",
  start: 0,
  end: 1200,
};
const output: TranscriptTurn = {
  id: "output",
  speaker: "output",
  text: "Let me check the current policy.",
  start: 1500,
  end: 2700,
};
const props = {
  activity: quietAudio,
  needsPlayback: false,
  meterUnavailable: false,
  connecting: false,
  working: false,
  muted: false,
};

describe("Focused live captions", () => {
  it("shows input deltas immediately and retains only the current turn", () => {
    const { rerender } = render(<LiveCaption {...props} turns={[input]} />);
    expect(screen.getByText(input.text)).toBeTruthy();
    const correction = {
      ...input,
      id: "correction",
      text: "Actually, compare March and September.",
      start: 4000,
      end: 5000,
    };
    rerender(<LiveCaption {...props} turns={[input, output, correction]} />);
    expect(screen.getByText(correction.text)).toBeTruthy();
    expect(screen.queryByText(input.text)).toBeNull();
    expect(screen.queryByText(output.text)).toBeNull();
  });

  it("waits for audible output before handing the caption stage to the assistant", () => {
    const { rerender } = render(<LiveCaption {...props} turns={[input]} />);
    rerender(<LiveCaption {...props} turns={[input, output]} />);
    expect(screen.queryByText(output.text)).toBeNull();
    expect(screen.getByText(input.text)).toBeTruthy();
    rerender(
      <LiveCaption
        {...props}
        activity={{ ...quietAudio, outputLevel: 0.2 }}
        turns={[input, output]}
      />,
    );
    expect(screen.getByText(output.text)).toBeTruthy();
    expect(screen.queryByText(input.text)).toBeNull();
  });

  it("clears interrupted output and does not redisplay its late fragments", () => {
    const { rerender } = render(
      <LiveCaption
        {...props}
        activity={{ ...quietAudio, outputLevel: 0.2 }}
        turns={[input, output]}
      />,
    );
    rerender(
      <LiveCaption
        {...props}
        activity={{ ...quietAudio, inputLevel: 0.2 }}
        turns={[input, output]}
      />,
    );
    expect(screen.queryByText(output.text)).toBeNull();
    rerender(
      <LiveCaption
        {...props}
        turns={[input, { ...output, text: "Late words from that answer." }]}
      />,
    );
    expect(screen.queryByText("Late words from that answer.")).toBeNull();
    const next = {
      ...input,
      id: "new-input",
      text: "No, the earlier version.",
      start: 3000,
      end: 4000,
    };
    rerender(<LiveCaption {...props} turns={[input, output, next]} />);
    expect(screen.getByText(next.text)).toBeTruthy();
  });

  it.each([{ needsPlayback: true }, { meterUnavailable: true }])(
    "keeps captions accessible when audio cannot drive the view: %j",
    (fallback) => {
      render(<LiveCaption {...props} {...fallback} turns={[input, output]} />);
      expect(screen.getByText(output.text)).toBeTruthy();
      if (fallback.needsPlayback)
        expect(screen.getByText("Sound paused")).toBeTruthy();
    },
  );

  it("rolls long text at word boundaries without rewriting identifiers, numbers or negation", () => {
    const prefix =
      "This is a longer question about the available source documents and their different versions. ";
    const text = prefix.repeat(3) + "PT-006 has no recorded treatment price.";
    const page = captionPage(text);
    expect(page.offset).toBeGreaterThan(0);
    expect(page.text).toBe(text.slice(page.offset).trim());
    expect(page.text).toContain("PT-006 has no recorded treatment price.");
    expect(page.text.length).toBeLessThanOrEqual(110);
    expect(captionPage("One repeated repeated word").text).toBe(
      "One repeated repeated word",
    );
  });

  it("keeps the full final sentence together instead of isolating its last words", () => {
    expect(
      captionPage(
        "The record notes an educational consultation. The record does not give a specific downtime duration.",
      ).text,
    ).toBe("The record does not give a specific downtime duration.");
  });

  it("clears old captions while a new session connects", () => {
    const { rerender } = render(<LiveCaption {...props} turns={[input]} />);
    rerender(<LiveCaption {...props} connecting turns={[input]} />);
    expect(screen.queryByText(input.text)).toBeNull();
    expect(screen.getByText("A moment to connect.")).toBeTruthy();
  });
});

import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { VoiceBar } from "../VoiceBar";
import type { useLive } from "../useLive";
import { quietAudio } from "../liveAudio";
import type { Run } from "../api";

const answeredRun: Run = {
  id: "voice-answer",
  conversation_id: "conversation",
  question: "What is the cancellation policy?",
  context_version: 1,
  index_id: "test-index",
  status: "completed",
  answer: {
    status: "answered",
    statements: [],
    gaps: [],
    next_steps: [],
    scopes: [],
    index_id: "test-index",
    index_checksum: "test-checksum",
    usage: [],
    diagnostics: {},
  },
  error: null,
  metrics: {},
  created_at: "2026-09-11T12:00:00Z",
  completed_at: "2026-09-11T12:00:05Z",
};

const live: ReturnType<typeof useLive> = {
  state: "listening",
  active: true,
  error: "",
  captions: { input: "What is the cancellation policy?", output: "" },
  transcript: [
    {
      id: "t1",
      speaker: "input",
      text: "What is the cancellation policy?",
      start: 0,
      end: 1,
    },
  ],
  audioActivity: quietAudio,
  visualizerError: "",
  transcriptTruncated: false,
  muted: false,
  needsPlayback: false,
  start: vi.fn(),
  end: vi.fn(),
  toggleMute: vi.fn(),
  resumePlayback: vi.fn(),
};
const actions = {
  onRetry: vi.fn(),
  onRefresh: vi.fn(),
  onUseQuestion: vi.fn(),
  onDismiss: vi.fn(),
};

describe("Nonmodal voice controls", () => {
  it("keeps captions in the page without duplicating the composer's voice controls", () => {
    render(<VoiceBar live={live} preparing={false} watching {...actions} />);
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(
      screen.getByRole("region", { name: "Live conversation" }),
    ).toBeTruthy();
    expect(screen.queryByRole("button", { name: /^End/ })).toBeNull();
    expect(
      screen.queryByRole("button", { name: "Mute microphone" }),
    ).toBeNull();
    expect(screen.getByText(/What is the cancellation policy/)).toBeTruthy();
  });

  it("preserves captions and explicit recovery after End instead of a disappearing panel", async () => {
    render(
      <VoiceBar
        live={{ ...live, active: false, state: "ended" }}
        preparing={false}
        watching={false}
        {...actions}
      />,
    );
    expect(screen.getByText("Voice ended")).toBeTruthy();
    expect(screen.getByText(/No answer was saved/)).toBeTruthy();
    await userEvent.click(
      screen.getByRole("button", { name: "Continue in chat" }),
    );
    expect(actions.onUseQuestion).toHaveBeenCalledOnce();
    expect(live.start).not.toHaveBeenCalled();
  });

  it("distinguishes pending saved-work synchronization from success", () => {
    render(
      <VoiceBar
        live={{ ...live, active: false, state: "ended" }}
        preparing={false}
        watching
        {...actions}
      />,
    );
    expect(screen.getByText(/Checking for your last answer/)).toBeTruthy();
    expect(screen.queryByText(/Answer saved in this chat/)).toBeNull();
  });

  it("describes a failed start without claiming a connection was established", () => {
    render(
      <VoiceBar
        live={{
          ...live,
          active: false,
          state: "error",
          error: "Microphone access was denied.",
        }}
        preparing={false}
        watching
        {...actions}
      />,
    );
    expect(screen.getByText("Voice unavailable")).toBeTruthy();
    expect(screen.getByRole("alert").textContent).toBe(
      "Microphone access was denied.",
    );
    expect(screen.queryByText("Voice recap")).toBeNull();
  });

  it("keeps live captions visible without a redundant caption toggle", () => {
    render(<VoiceBar live={live} preparing={false} watching {...actions} />);
    expect(
      screen.queryByRole("button", { name: /Hide transcript|Show transcript/ }),
    ).toBeNull();
    expect(
      screen.getByRole("region", { name: "Live transcript" }),
    ).toBeTruthy();
    expect(screen.getByText(live.captions.input)).toBeTruthy();
  });

  it("keeps an explicitly reopened recap open across saved-answer polling updates", async () => {
    const user = userEvent.setup();
    const ended = { ...live, active: false, state: "ended" as const };
    const { rerender } = render(
      <VoiceBar
        live={ended}
        preparing={false}
        watching
        run={answeredRun}
        summaryReady
        {...actions}
      />,
    );
    await user.click(screen.getByRole("button", { name: "Show transcript" }));
    rerender(
      <VoiceBar
        live={ended}
        preparing={false}
        watching
        run={structuredClone(answeredRun)}
        summaryReady
        {...actions}
      />,
    );
    expect(
      screen.getByRole("region", { name: "Live transcript" }),
    ).toBeTruthy();
  });

  it.each(["focused", "scrolled"] as const)(
    "does not collapse a %s transcript when the answer arrives",
    (reading) => {
      const ended = { ...live, active: false, state: "ended" as const };
      const { rerender } = render(
        <VoiceBar live={ended} preparing={false} watching {...actions} />,
      );
      const transcript = screen.getByRole("region", {
        name: "Live transcript",
      });
      if (reading === "focused") {
        transcript.focus();
      } else {
        Object.defineProperties(transcript, {
          scrollHeight: { value: 500 },
          clientHeight: { value: 150 },
          scrollTop: { value: 0, writable: true },
        });
        fireEvent.scroll(transcript);
      }
      rerender(
        <VoiceBar
          live={ended}
          preparing={false}
          watching
          run={answeredRun}
          {...actions}
        />,
      );
      expect(
        screen.getByRole("region", { name: "Live transcript" }),
      ).toBeTruthy();
    },
  );

  it("preserves focus in the retained transcript across the fallback End state", () => {
    const { rerender } = render(
      <VoiceBar live={live} preparing={false} watching {...actions} />,
    );
    const transcript = screen.getByRole("region", { name: "Live transcript" });
    transcript.focus();
    rerender(
      <VoiceBar
        live={{ ...live, active: false, state: "ended" }}
        preparing={false}
        watching
        {...actions}
      />,
    );
    expect(document.activeElement).toBe(transcript);
  });
});

describe("Live voice presentation refinements", () => {
  it("lets the user review the full transcript during Live without starting another session", async () => {
    const output = {
      id: "t2",
      speaker: "output" as const,
      text: "The source describes the current policy.",
      start: 2,
      end: 3,
    };
    render(
      <VoiceBar
        live={{ ...live, transcript: [...live.transcript, output] }}
        preparing={false}
        watching
        {...actions}
      />,
    );
    await userEvent.click(
      screen.getByRole("button", { name: "Transcript", exact: true }),
    );
    expect(screen.getByText(live.captions.input)).toBeTruthy();
    expect(screen.getByText(output.text)).toBeTruthy();
    expect(
      screen.getByText("Transcript text can arrive before the spoken audio."),
    ).toBeTruthy();
    expect(live.start).not.toHaveBeenCalled();
    await userEvent.click(
      screen.getByRole("button", { name: "Focus", exact: true }),
    );
    expect(
      screen.queryByText("Transcript text can arrive before the spoken audio."),
    ).toBeNull();
  });
  it("does not drag the reader to the end while they inspect earlier live text", async () => {
    const { rerender } = render(
      <VoiceBar live={live} preparing={false} watching {...actions} />,
    );
    await userEvent.click(
      screen.getByRole("button", { name: "Transcript", exact: true }),
    );
    const panel = screen.getByRole("region", { name: "Live transcript" });
    Object.defineProperties(panel, {
      scrollHeight: { value: 800 },
      clientHeight: { value: 150 },
      scrollTop: { value: 50, writable: true },
    });
    fireEvent.scroll(panel);
    rerender(
      <VoiceBar
        live={{
          ...live,
          transcript: [
            ...live.transcript,
            {
              id: "later",
              speaker: "input",
              text: "Another question.",
              start: 4,
              end: 5,
            },
          ],
        }}
        preparing={false}
        watching
        {...actions}
      />,
    );
    expect(panel.scrollTop).toBe(50);
    await userEvent.click(
      screen.getByRole("button", { name: "Jump to latest" }),
    );
    expect(panel.scrollTop).toBe(800);
  });
  it("keeps paused sound distinct from listening and suppresses output animation", () => {
    render(
      <VoiceBar
        live={{
          ...live,
          needsPlayback: true,
          audioActivity: {
            ...quietAudio,
            outputLevel: 0.8,
            output: Array(9).fill(0.8),
          },
        }}
        preparing={false}
        watching
        {...actions}
      />,
    );
    expect(screen.getByText("Sound is paused")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Enable sound" })).toBeTruthy();
    expect(
      document.querySelector(".voice-orbit")?.getAttribute("data-state"),
    ).toBe("sound-paused");
    expect(
      [...document.querySelectorAll(".voice-ring-bars line")].every(
        (line) => line.getAttribute("y2") === "22",
      ),
    ).toBe(true);
  });
});

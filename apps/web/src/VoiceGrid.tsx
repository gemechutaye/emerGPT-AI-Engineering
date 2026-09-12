import { useId } from "react";
import type { AudioActivity } from "./liveAudio";
import { Brand } from "./Brand";

export type VoicePresenceState =
  | "connecting"
  | "listening"
  | "working"
  | "speaking"
  | "muted"
  | "sound-paused"
  | "ended"
  | "error";

/** Frequency-driven radial presence; idle and connection motion are separate cues. */
export function VoiceGrid({
  activity,
  state,
}: {
  activity: AudioActivity;
  state: VoicePresenceState;
}) {
  const gradient = useId();
  const output = state === "speaking";
  const audible = output || state === "listening";
  const bands = output ? activity.output : activity.input;
  const level = audible
    ? output
      ? activity.outputLevel
      : activity.inputLevel
    : 0;
  return (
    <div
      className={`voice-field voice-orbit ${state}`}
      data-state={state}
      aria-hidden="true"
    >
      <div
        className="voice-orbit-halo"
        style={{ opacity: 0.15 + level * 0.5 }}
      />
      <svg viewBox="0 0 200 200" className="voice-ring">
        <defs>
          <linearGradient
            id={gradient}
            x1="0"
            y1="0"
            x2="200"
            y2="200"
            gradientUnits="userSpaceOnUse"
          >
            <stop stopColor="var(--voice-ring-start)" />
            <stop offset="1" stopColor="var(--voice-ring-end)" />
          </linearGradient>
        </defs>
        <circle className="voice-orbit-track" cx="100" cy="100" r="64" />
        <g className="voice-ring-bars">
          {Array.from({ length: 48 }, (_, index) => {
            const strength = audible
              ? Math.max(0, Math.min(1, bands[index % bands.length] ?? 0))
              : 0;
            return (
              <line
                key={index}
                x1="100"
                y1="27"
                x2="100"
                y2={22 - strength * 19}
                transform={`rotate(${index * 7.5} 100 100)`}
                stroke={`url(#${gradient})`}
                strokeWidth="3.4"
                strokeLinecap="round"
                opacity={0.32 + strength * 0.68}
              />
            );
          })}
        </g>
      </svg>
      <span className="voice-ring-mark">
        <Brand />
      </span>
    </div>
  );
}

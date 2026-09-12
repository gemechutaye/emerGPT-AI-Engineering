import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

/** The reference experience is mounted only while the new-chat homepage exists. */
export function HomeDisco() {
  const ball = useRef<HTMLButtonElement>(null);
  const sound = useRef<HTMLButtonElement>(null);
  const status = useRef<HTMLParagraphElement>(null);
  const [controls, setControls] = useState<HTMLElement | null>(null);

  useEffect(() => {
    setControls(document.getElementById("home-disco-controls"));
  }, []);

  useEffect(() => {
    if (!controls) return;
    let cancelled = false;
    let dispose: (() => void) | undefined;
    const entry = "/assets/home-disco/homepage-party.mjs";
    void import(/* @vite-ignore */ entry)
      .then((party) => {
        if (cancelled || !ball.current || !sound.current || !status.current)
          return;
        dispose = party.mountParty(ball.current, sound.current, status.current);
      })
      .catch(() => {
        if (!cancelled && status.current)
          status.current.textContent =
            "The disco is taking a break. Reload to try again.";
      });
    return () => {
      cancelled = true;
      dispose?.();
    };
  }, [controls]);

  return (
    <div className="home-disco">
      <button
        ref={ball}
        type="button"
        className="disco home-disco-ball"
        aria-label="Disco ball. Tap for confetti; drag to spin."
        aria-describedby="home-disco-hint"
      >
        <img
          src="/assets/home-disco/disco-ball.png"
          width="1254"
          height="1254"
          alt="A silver mirrored disco ball"
          draggable={false}
          fetchPriority="high"
        />
      </button>
      <span id="home-disco-hint" className="sr-only">
        Drag to spin. Left and right arrow keys fling; Escape slows the ball.
        Enter or Space celebrates.
      </span>
      <p ref={status} className="home-disco-status" role="status" />
      {controls &&
        createPortal(
          <button
            ref={sound}
            type="button"
            className="home-disco-sound"
            aria-pressed="false"
          >
            Tap for sound
          </button>,
          controls,
        )}
    </div>
  );
}

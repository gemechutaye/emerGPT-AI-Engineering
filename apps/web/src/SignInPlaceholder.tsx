import { useState } from "react";
import { Dialog } from "./Dialog";
import { Icon } from "./Icon";

export function SignInPlaceholder({
  floating = false,
}: {
  floating?: boolean;
}) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button
        className={`sign-in-entry ${floating ? "is-floating" : ""}`}
        onClick={() => setOpen(true)}
        aria-haspopup="dialog"
      >
        <span className="sign-in-avatar">
          <Icon name="user" size={18} />
        </span>
        <span>Sign In</span>
        <Icon name="chevron" size={14} />
      </button>
      {open && (
        <Dialog
          title="Your space, coming soon."
          className="sign-in-dialog"
          onClose={() => setOpen(false)}
        >
          <div className="sign-in-art" aria-hidden="true">
            <span className="sign-in-orbit orbit-one" />
            <span className="sign-in-orbit orbit-two" />
            <span className="sign-in-emblem">
              <Icon name="spark" size={46} />
            </span>
            <span className="sign-in-art-caption">
              More room
              <br />
              for you.
            </span>
          </div>
          <div className="sign-in-copy">
            <span className="coming-soon">Coming soon</span>
            <p>
              A home for your conversations, ideas and everything in between.
            </p>
            <button className="sign-in-unavailable" disabled>
              Sign in · Coming soon
            </button>
            <p className="sign-in-note">
              Sign-in and account creation aren’t available yet.
            </p>
            <button
              className="continue-exploring"
              onClick={() => setOpen(false)}
            >
              Keep exploring <Icon name="chevron" size={15} />
            </button>
          </div>
        </Dialog>
      )}
    </>
  );
}

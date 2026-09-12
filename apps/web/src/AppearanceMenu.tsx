import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { Icon } from "./Icon";

type Theme = "light" | "dark" | "system";
const storageKey = "emer:theme";
function savedTheme(): Theme {
  try {
    const value = localStorage.getItem(storageKey);
    if (value === "light" || value === "dark") return value;
  } catch {
    // Appearance still works when browser storage is unavailable.
  }
  return "system";
}
function applyTheme(theme: Theme) {
  const dark =
    theme === "dark" ||
    (theme === "system" &&
      window.matchMedia?.("(prefers-color-scheme: dark)").matches);
  document.documentElement.dataset.theme = dark ? "dark" : "light";
  document.documentElement.style.colorScheme = dark ? "dark" : "light";
  document
    .querySelector('meta[name="theme-color"]')
    ?.setAttribute("content", dark ? "#171615" : "#fafafa");
}

// Apply the saved preference before React renders, including shared chat pages.
export function initializeAppearance() {
  applyTheme(savedTheme());
}

export function AppearanceMenu() {
  const [theme, setTheme] = useState<Theme>(savedTheme);
  const [mode, setMode] = useState<"closed" | "hover" | "click">("closed");
  const open = mode !== "closed";
  const closeTimer = useRef<ReturnType<typeof setTimeout> | undefined>(
    undefined,
  );
  const keyboardOpen = useRef(false);
  const cancelClose = () => {
    clearTimeout(closeTimer.current);
  };
  const close = () => {
    cancelClose();
    setMode("closed");
  };
  useEffect(() => () => clearTimeout(closeTimer.current), []);
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  useLayoutEffect(() => {
    applyTheme(theme);
    const media = window.matchMedia?.("(prefers-color-scheme: dark)");
    const update = () => applyTheme(theme);
    media?.addEventListener("change", update);
    return () => media?.removeEventListener("change", update);
  }, [theme]);
  useEffect(() => {
    const sync = (event: StorageEvent) => {
      if (event.key === storageKey || event.key === null)
        setTheme(savedTheme());
    };
    window.addEventListener("storage", sync);
    return () => window.removeEventListener("storage", sync);
  }, []);
  useLayoutEffect(() => {
    if (open && keyboardOpen.current)
      root.current?.querySelector<HTMLInputElement>("input:checked")?.focus();
  }, [open, mode]);
  useEffect(() => {
    if (!open) return;
    const outside = (event: PointerEvent) => {
      if (!root.current?.contains(event.target as Node)) close();
    };
    document.addEventListener("pointerdown", outside);
    return () => document.removeEventListener("pointerdown", outside);
  }, [open]);
  return (
    <div
      className="appearance-control"
      ref={root}
      onPointerEnter={(event) => {
        if (event.pointerType !== "mouse") return;
        cancelClose();
        keyboardOpen.current = false;
        setMode((current) => (current === "closed" ? "hover" : current));
      }}
      onPointerLeave={() => {
        if (mode !== "hover") return;
        cancelClose();
        closeTimer.current = setTimeout(() => setMode("closed"), 180);
      }}
      onKeyDown={(event) => {
        if (event.key === "Escape" && open) {
          event.stopPropagation();
          close();
          trigger.current?.focus();
        }
      }}
      onBlur={(event) => {
        // A label click can briefly blur to no target before its radio focuses.
        // Outside pointer presses are already handled separately.
        if (
          event.relatedTarget &&
          !event.currentTarget.contains(event.relatedTarget)
        )
          close();
      }}
    >
      <button
        ref={trigger}
        className="quiet-icon appearance-trigger"
        aria-label="Appearance settings"
        aria-expanded={open}
        aria-controls="appearance-options"
        onClick={(event) => {
          cancelClose();
          keyboardOpen.current = event.detail === 0;
          setMode((current) => (current === "click" ? "closed" : "click"));
        }}
      >
        <Icon name="settings" size={20} />
      </button>
      {open && (
        <fieldset className="appearance-menu" id="appearance-options">
          <legend>Appearance</legend>
          <div className="theme-options">
            {(["light", "dark", "system"] as const).map((value) => (
              <label key={value}>
                <input
                  type="radio"
                  name="theme"
                  value={value}
                  checked={theme === value}
                  onChange={() => {
                    setTheme(value);
                    try {
                      localStorage.setItem(storageKey, value);
                    } catch {
                      // Keep the choice for this page even without persistence.
                    }
                  }}
                />
                <span>
                  <Icon
                    name={
                      value === "light"
                        ? "sun"
                        : value === "dark"
                          ? "moon"
                          : "monitor"
                    }
                    size={16}
                  />
                  {value[0].toUpperCase() + value.slice(1)}
                </span>
              </label>
            ))}
          </div>
        </fieldset>
      )}
    </div>
  );
}

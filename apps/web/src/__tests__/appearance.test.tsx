import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import { AppearanceMenu, initializeAppearance } from "../AppearanceMenu";
import { SignInPlaceholder } from "../SignInPlaceholder";

let dark = false;
let changed: (() => void) | undefined;
beforeEach(() => {
  dark = false;
  changed = undefined;
  vi.stubGlobal("matchMedia", () => ({
    get matches() {
      return dark;
    },
    addEventListener: (_: string, listener: () => void) => {
      changed = listener;
    },
    removeEventListener: vi.fn(),
  }));
});

it("restores the saved theme before rendering and persists a changed choice", async () => {
  localStorage.setItem("emer:theme", "dark");
  initializeAppearance();
  expect(document.documentElement.dataset.theme).toBe("dark");
  render(<AppearanceMenu />);
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "Appearance settings" }));
  await user.click(screen.getByRole("radio", { name: "Light" }));
  expect(localStorage.getItem("emer:theme")).toBe("light");
  expect(document.documentElement.dataset.theme).toBe("light");
  await user.keyboard("{Escape}");
  expect(screen.queryByRole("group", { name: "Appearance" })).toBeNull();
  expect(document.activeElement).toBe(
    screen.getByRole("button", { name: "Appearance settings" }),
  );
});

it("follows system changes only in System mode and receives another tab's choice", async () => {
  render(<AppearanceMenu />);
  act(() => {
    dark = true;
    changed?.();
  });
  expect(document.documentElement.dataset.theme).toBe("dark");
  localStorage.setItem("emer:theme", "light");
  fireEvent(
    window,
    new StorageEvent("storage", { key: "emer:theme", newValue: "light" }),
  );
  expect(document.documentElement.dataset.theme).toBe("light");
  act(() => changed?.());
  expect(document.documentElement.dataset.theme).toBe("light");
});

it("dismisses appearance on an outside press", async () => {
  render(
    <>
      <AppearanceMenu />
      <button>Outside</button>
    </>,
  );
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "Appearance settings" }));
  await user.click(screen.getByRole("button", { name: "Outside" }));
  expect(screen.queryByRole("group", { name: "Appearance" })).toBeNull();
});

it("keeps radio labels mounted across the temporary blur during a label click", async () => {
  render(<AppearanceMenu />);
  const trigger = screen.getByRole("button", { name: "Appearance settings" });
  await userEvent.click(trigger);
  fireEvent.blur(trigger, { relatedTarget: null });
  await userEvent.click(screen.getByText("Dark", { exact: true }));
  expect(document.documentElement.dataset.theme).toBe("dark");
  expect(screen.getByRole("radio", { name: "Dark" }).checked).toBe(true);
});

it("allows choosing appearance even when preference storage is blocked", async () => {
  vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
    throw new Error("blocked");
  });
  const setter = vi
    .spyOn(Storage.prototype, "setItem")
    .mockImplementation(() => {
      throw new Error("blocked");
    });
  render(<AppearanceMenu />);
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "Appearance settings" }));
  await user.click(screen.getByRole("radio", { name: "Dark" }));
  expect(document.documentElement.dataset.theme).toBe("dark");
  setter.mockRestore();
  vi.restoreAllMocks();
});

it("opens a credential-free placeholder and returns focus when dismissed", async () => {
  const fetch = vi.fn();
  vi.stubGlobal("fetch", fetch);
  render(<SignInPlaceholder />);
  const user = userEvent.setup();
  const trigger = screen.getByRole("button", { name: "Sign In" });
  await user.click(trigger);
  const dialog = screen.getByRole("dialog", {
    name: "Your space, coming soon.",
  });
  expect(dialog.querySelector("form,input,a")).toBeNull();
  expect(
    screen
      .getByRole("button", { name: "Sign in · Coming soon" })
      .hasAttribute("disabled"),
  ).toBe(true);
  await user.click(screen.getByRole("button", { name: "Keep exploring" }));
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(document.activeElement).toBe(trigger);
  expect(fetch).not.toHaveBeenCalled();
});

it("opens on hover without stealing focus and closes after leaving the menu", async () => {
  render(
    <>
      <input aria-label="Question" />
      <AppearanceMenu />
    </>,
  );
  const input = screen.getByRole("textbox");
  input.focus();
  const user = userEvent.setup();
  await user.hover(screen.getByRole("button", { name: "Appearance settings" }));
  expect(screen.getByRole("group", { name: "Appearance" })).toBeTruthy();
  expect(document.activeElement).toBe(input);
  await user.hover(screen.getByRole("radio", { name: "Dark" }));
  await user.click(screen.getByRole("radio", { name: "Dark" }));
  expect(document.documentElement.dataset.theme).toBe("dark");
  await user.unhover(screen.getByRole("radio", { name: "Dark" }));
  await waitFor(() =>
    expect(screen.queryByRole("group", { name: "Appearance" })).toBeNull(),
  );
});

it("a click pins a hover-open menu and all three options apply the intended theme", async () => {
  render(
    <>
      <AppearanceMenu />
      <button>Outside</button>
    </>,
  );
  const user = userEvent.setup();
  const trigger = screen.getByRole("button", { name: "Appearance settings" });
  await user.hover(trigger);
  await user.click(trigger);
  expect(screen.getByRole("group", { name: "Appearance" })).toBeTruthy();
  await user.click(screen.getByRole("radio", { name: "Dark" }));
  expect(document.documentElement.dataset.theme).toBe("dark");
  await user.click(screen.getByRole("radio", { name: "Light" }));
  expect(document.documentElement.dataset.theme).toBe("light");
  dark = true;
  await user.click(screen.getByRole("radio", { name: "System" }));
  expect(document.documentElement.dataset.theme).toBe("dark");
  act(() => {
    dark = false;
    changed?.();
  });
  expect(document.documentElement.dataset.theme).toBe("light");
  expect(localStorage.getItem("emer:theme")).toBe("system");
  await user.hover(screen.getByRole("button", { name: "Outside" }));
  expect(screen.getByRole("group", { name: "Appearance" })).toBeTruthy();
  await user.click(screen.getByRole("button", { name: "Outside" }));
  expect(screen.queryByRole("group", { name: "Appearance" })).toBeNull();
});

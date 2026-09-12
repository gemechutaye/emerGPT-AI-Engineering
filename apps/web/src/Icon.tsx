// Selected paths adapted from Lucide/Feather; see public/assets/licenses/lucide.txt.
import type { CSSProperties } from "react";
type Name =
  | "sidebar"
  | "compose"
  | "command"
  | "more"
  | "pin"
  | "share"
  | "edit"
  | "trash"
  | "link"
  | "plus"
  | "arrow"
  | "arrow-right"
  | "mic"
  | "stop"
  | "book"
  | "chevron"
  | "close"
  | "code"
  | "file"
  | "check"
  | "search"
  | "menu"
  | "refresh"
  | "spark"
  | "clock"
  | "patient"
  | "settings"
  | "sun"
  | "moon"
  | "monitor"
  | "user"
  | "audio"
  | "mic-off"
  | "phone-end"
  | "minimize"
  | "expand"
  | "chat"
  | "headphones";
const paths: Record<Name, React.ReactNode> = {
  "arrow-right": <path d="M5 12h14m-6-6 6 6-6 6" />,
  sun: (
    <>
      <circle cx="12" cy="12" r="4" />
      <path d="M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1.5 1.5m11 11L19 19M5 19l1.5-1.5m11-11L19 5" />
    </>
  ),
  moon: <path d="M20.5 14.2A8.8 8.8 0 0 1 9.8 3.5a8.8 8.8 0 1 0 10.7 10.7Z" />,
  monitor: (
    <>
      <rect x="3" y="4" width="18" height="13" rx="2" />
      <path d="M8 21h8m-4-4v4" />
    </>
  ),
  user: (
    <>
      <circle cx="12" cy="8" r="3" />
      <path d="M5 20v-2a7 7 0 0 1 14 0v2" />
      <circle cx="12" cy="12" r="10" />
    </>
  ),
  sidebar: (
    <>
      <rect x="3" y="4" width="18" height="16" rx="3" />
      <path d="M9 4v16" />
    </>
  ),
  compose: (
    <>
      <path d="M12 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7" />
      <path d="M18.375 2.625a1 1 0 0 1 3 3l-9.013 9.014a2 2 0 0 1-.853.505l-2.873.84a.5.5 0 0 1-.62-.62l.84-2.873a2 2 0 0 1 .506-.852z" />
    </>
  ),
  command: (
    <path d="M15 6v12a3 3 0 1 0 3-3H6a3 3 0 1 0 3 3V6a3 3 0 1 0-3 3h12a3 3 0 1 0-3-3" />
  ),
  more: (
    <>
      <circle cx="5" cy="12" r="1" fill="currentColor" />
      <circle cx="12" cy="12" r="1" fill="currentColor" />
      <circle cx="19" cy="12" r="1" fill="currentColor" />
    </>
  ),
  pin: (
    <>
      <path d="M12 17v5" />
      <path d="M9 10.76a2 2 0 0 1-1.11 1.79l-1.78.9A2 2 0 0 0 5 15.24V16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-.76a2 2 0 0 0-1.11-1.79l-1.78-.9A2 2 0 0 1 15 10.76V7a1 1 0 0 1 1-1 2 2 0 0 0 0-4H8a2 2 0 0 0 0 4 1 1 0 0 1 1 1z" />
    </>
  ),
  share: (
    <>
      <path d="M12 16V3m-4 4 4-4 4 4M6 11H4v10h16V11h-2" />
    </>
  ),
  edit: <path d="m15 4 5 5M4 20l5-1L21 7a2 2 0 0 0-4-4L5 15Z" />,
  trash: (
    <>
      <path d="M3 6h18M9 6V3h6v3M6 6l1 15h10l1-15M10 10v7m4-7v7" />
    </>
  ),
  link: (
    <>
      <path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71" />
      <path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71" />
    </>
  ),
  audio: <path d="M4 10v4m4-7v10m4-14v18m4-14v10m4-7v4" />,
  "mic-off": (
    <>
      <path d="M9 9v3a3 3 0 0 0 5 2M9 5a3 3 0 0 1 6 1v4M6 11v1a6 6 0 0 0 10 4M18 11v1m-6 6v3m-3 0h6M3 3l18 18" />
    </>
  ),
  "phone-end": <path d="M3 14v3l5-1v-3m8 0v3l5 1v-3c-5-6-13-6-18 0Z" />,
  minimize: <path d="M5 12h14" />,
  expand: <path d="M8 3H3v5m13-5h5v5M3 16v5h5m13-5v5h-5" />,
  chat: (
    <path d="M2.992 16.342a2 2 0 0 1 .094 1.167l-1.065 3.29a1 1 0 0 0 1.236 1.168l3.413-.998a2 2 0 0 1 1.099.092 10 10 0 1 0-4.777-4.719" />
  ),
  plus: <path d="M12 5v14M5 12h14" />,
  patient: (
    <>
      <circle cx="12" cy="8" r="3.5" />
      <path d="M5 21v-3a7 7 0 0 1 14 0v3" />
    </>
  ),
  settings: (
    <>
      <path d="m9 4.3.6-2.3h4.8l.6 2.3 2 1.2 2.3-.6 2.4 4.2-1.7 1.7v2.4l1.7 1.7-2.4 4.2-2.3-.6-2 1.2-.6 2.3H9.6L9 19.7l-2-1.2-2.3.6-2.4-4.2L4 13.2v-2.4L2.3 9.1l2.4-4.2 2.3.6Z" />
      <circle cx="12" cy="12" r="3.2" />
    </>
  ),
  arrow: (
    <>
      <path d="M12 19V5m-6 6 6-6 6 6" />
    </>
  ),
  mic: (
    <>
      <rect x="9" y="3" width="6" height="12" rx="3" />
      <path d="M6 11v1a6 6 0 0 0 12 0v-1m-6 7v3m-3 0h6" />
    </>
  ),
  stop: <rect x="6" y="6" width="12" height="12" rx="2" />,
  book: (
    <>
      <path d="M12 5v15m0-15C8 2 4 3 3 4v15c2-1 6-1 9 1 3-2 7-2 9-1V4c-2-1-6-2-9 1Z" />
    </>
  ),
  chevron: <path d="m9 5 7 7-7 7" />,
  close: <path d="m6 6 12 12M6 18 18 6" />,
  code: (
    <>
      <path d="m8 7-5 5 5 5m8-10 5 5-5 5m-3-14-2 18" />
    </>
  ),
  file: (
    <>
      <path d="M14 3H5v18h14V8l-5-5Zm0 0v5h5M8 12h8m-8 4h6" />
    </>
  ),
  check: <path d="m5 12 4 4L19 6" />,
  search: (
    <>
      <circle cx="10" cy="10" r="6" />
      <path d="m15 15 5 5" />
    </>
  ),
  menu: <path d="M4 6h16M4 12h16M4 18h16" />,
  refresh: (
    <>
      <path d="M20 10a8 8 0 1 0-2 8M20 4v6h-6" />
    </>
  ),
  spark: (
    <>
      <path d="m12 2 2.8 7.2L22 12l-7.2 2.8L12 22l-2.8-7.2L2 12l7.2-2.8L12 2Z" />
    </>
  ),
  clock: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M12 7v5l3 2" />
    </>
  ),
  headphones: (
    <>
      <path d="M4 13v-2a8 8 0 0 1 16 0v2" />
      <rect x="3" y="12" width="4" height="8" rx="2" />
      <rect x="17" y="12" width="4" height="8" rx="2" />
    </>
  ),
};
export function Icon({
  name,
  size = 18,
  style,
}: {
  name: Name;
  size?: number;
  style?: CSSProperties;
}) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={
        ["compose", "command", "pin", "link", "chat"].includes(name) ? 1.8 : 1.6
      }
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      style={style}
    >
      {paths[name]}
    </svg>
  );
}

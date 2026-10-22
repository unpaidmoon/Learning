import type { CSSProperties } from "react";

type IconName =
  | "folder"
  | "file"
  | "chat"
  | "plus"
  | "close"
  | "sun"
  | "moon"
  | "settings"
  | "panel"
  | "capture"
  | "chevron"
  | "more"
  | "video"
  | "back"
  | "forward"
  | "up"
  | "refresh"
  | "grid"
  | "list"
  | "copy"
  | "cut"
  | "paste"
  | "rename"
  | "trash"
  | "info"
  | "chevron-down"
  | "file-text"
  | "file-pdf";
const paths: Record<IconName, string> = {
  folder: "M3 7V5a1 1 0 0 1 1-1h5l2 2h9a1 1 0 0 1 1 1v12a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V7Z",
  file: "M14 3H6a1 1 0 0 0-1 1v16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V8l-5-5Zm0 0v5h5M8 12h8M8 16h6",
  chat: "M21 11a8 8 0 0 1-8 8H8l-5 3V11a8 8 0 0 1 8-8h2a8 8 0 0 1 8 8ZM8 9h8M8 13h5",
  plus: "M12 5v14M5 12h14",
  close: "m6 6 12 12M18 6 6 18",
  sun: "M12 3v2m0 14v2M3 12h2m14 0h2M5.6 5.6 7 7m10 10 1.4 1.4m0-12.8L17 7M7 17l-1.4 1.4M16 12a4 4 0 1 1-8 0 4 4 0 0 1 8 0Z",
  moon: "M20.4 14.4A8.5 8.5 0 0 1 9.6 3.6a8.5 8.5 0 1 0 10.8 10.8Z",
  settings:
    "m10 3-.7 2.3-2 .9-2.3-.5-2 3.6 1.6 1.8v2l-1.6 1.8 2 3.6 2.3-.5 2 .9.7 2.1h4l.7-2.1 2-.9 2.3.5 2-3.6-1.6-1.8v-2L21 9.3l-2-3.6-2.3.5-2-.9L14 3h-4ZM15 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0Z",
  panel: "M3 5a1 1 0 0 1 1-1h16a1 1 0 0 1 1 1v14a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V5Zm12-1v16",
  capture:
    "M8 3H4a1 1 0 0 0-1 1v4m13-5h4a1 1 0 0 1 1 1v4M3 16v4a1 1 0 0 0 1 1h4m8 0h4a1 1 0 0 0 1-1v-4M7 7h10v10H7V7Z",
  chevron: "m9 5 7 7-7 7",
  more: "M5 12h.01M12 12h.01M19 12h.01",
  video: "m10 8 6 4-6 4V8ZM4 4h16a1 1 0 0 1 1 1v14a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1Z",
  back: "m14 5-7 7 7 7M7 12h14",
  forward: "m10 5 7 7-7 7M3 12h14",
  up: "m5 10 7-7 7 7M12 3v18",
  refresh: "M20 7V3l-3 3a8 8 0 1 0 3 8M20 3h-5",
  grid: "M3 3h7v7H3V3Zm11 0h7v7h-7V3ZM3 14h7v7H3v-7Zm11 0h7v7h-7v-7Z",
  list: "M8 5h13M8 12h13M8 19h13M3 5h.01M3 12h.01M3 19h.01",
  copy: "M8 8h12v12H8V8ZM16 8V4H4v12h4",
  cut: "M9 9 20 3M9 15l11 6M4 7a3 3 0 1 0 0 6 3 3 0 0 0 0-6Zm0 7a3 3 0 1 0 0 6 3 3 0 0 0 0-6Z",
  paste: "M8 5H5v16h14V5h-3M8 3h8v4H8V3Z",
  rename: "m4 16 11-11 4 4L8 20H4v-4ZM13 7l4 4M14 21h7",
  trash: "M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7M14 10v7",
  info: "M12 11v6M12 7h.01M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z",
  "chevron-down": "m5 9 7 7 7-7",
  "file-text": "M14 3H5v18h14V8l-5-5Zm0 0v5h5M8 12h8M8 16h8",
  "file-pdf": "M14 3H5v18h14V8l-5-5Zm0 0v5h5M8 12h8M8 16h5",
};
export function Icon({
  name,
  size = 18,
  style,
}: {
  name: IconName;
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
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      style={style}
    >
      <path d={paths[name]} />
    </svg>
  );
}

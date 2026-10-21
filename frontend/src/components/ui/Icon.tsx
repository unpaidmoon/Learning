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
  | "video";
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

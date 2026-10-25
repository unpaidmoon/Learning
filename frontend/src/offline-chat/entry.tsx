import { createRoot } from "react-dom/client";
import { OfflineChatReader } from "./OfflineChatReader";
import type { OfflineChatIndex, OfflineChatPage } from "./model";

const target = document.getElementById("learning-offline-chat-root");
const raw = document.getElementById("learning-offline-chat-data")?.textContent;
if (!target || !raw) throw new Error("OFFLINE_CHAT_MISSING");
const value = JSON.parse(raw) as OfflineChatIndex | OfflineChatPage;
if (!(
  (value.format === "learning-offline-chat-index-v1" && Array.isArray(value.courses)) ||
  (value.format === "learning-offline-chat-v1" && Array.isArray(value.messages))
)) {
  throw new Error("OFFLINE_CHAT_FORMAT_UNSUPPORTED");
}
createRoot(target).render(<OfflineChatReader document={value} />);

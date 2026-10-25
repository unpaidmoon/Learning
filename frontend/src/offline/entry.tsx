import { createRoot } from "react-dom/client";
import { OfflineReader } from "./OfflineReader";
import type { OfflineDocument } from "./model";

const target = document.getElementById("learning-offline-root");
const raw = document.getElementById("learning-offline-data")?.textContent;
if (!target || !raw) throw new Error("OFFLINE_DOCUMENT_MISSING");
const value = JSON.parse(raw) as OfflineDocument;
if (value.format !== "learning-offline-v1" || !Array.isArray(value.blocks)) {
  throw new Error("OFFLINE_FORMAT_UNSUPPORTED");
}
createRoot(target).render(<OfflineReader document={value} />);

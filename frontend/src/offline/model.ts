import type { Schema } from "../api/client";

export type OfflineBlock = Schema<"ArtifactView">["revision"]["body"]["blocks"][number];
export type OfflineSource = {
  id: string;
  label: string;
  role: string;
  text: string;
  location: Record<string, unknown>;
  image?: string;
};
export type OfflineDocument = {
  format: "learning-offline-v1";
  title: string;
  kind: string;
  role: string;
  exported_at: string;
  revision_hash: string;
  coverage: string;
  notices?: string[];
  block_notices?: Record<string, string>;
  blocks: OfflineBlock[];
  sources: OfflineSource[];
  linked_questions: Record<string, OfflineBlock[]>;
  chapters: {
    id: string;
    title: string;
    objective: string;
    pages: { id: string; title: string; block_ids: string[] }[];
  }[];
};

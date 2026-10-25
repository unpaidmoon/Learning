import type { Schema } from "../api/client";

export type SubtitleActionHandler = (value: {
  course_id: string;
  track_id: string;
  capture: Schema<"SubtitleCapture">;
}) => Promise<Schema<"SourceSnapshot">>;

export type ReadingPosition = Pick<
  Schema<"ReadingState">,
  "page_index" | "scale" | "rotation" | "scroll_fraction"
>;

export type ReadingAction = {
  intent: "quote" | "translation" | "aligned_translation";
  operation_id: string;
  file: Schema<"CourseFileOpenResponse">;
  selection: Schema<"PdfSelectionInput">;
  force_regenerate?: boolean;
};

export type SourceJump = {
  request_id: string;
  reference: Schema<"DocumentSelectionRef"> | Schema<"DocumentRef">;
};

export type ReadingActionHandler = (
  value: ReadingAction,
) => Promise<Schema<"SourceSnapshot"> | Schema<"TaskView">>;

import type { Schema } from "../api/client";

export type OcrLayerAdapter = {
    apply(payload: Schema<"OcrPageLayer">, canReplace?: () => boolean): Promise<boolean>;
    dispose(): void;
};
export function attachOcrAdapter(
    application: unknown,
    expectedSourceVersion: string,
    onError?: (error: unknown) => void,
): Promise<OcrLayerAdapter>;

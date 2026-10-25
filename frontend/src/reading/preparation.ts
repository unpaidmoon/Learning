import { api } from "../api/client";
import type { Schema } from "../api/client";
import type { PdfReaderAdapter } from "./pdfAdapter";

export type PreparationOverview = Schema<"CurrentPagePreparation">;

export function readerIntent() {
  const clientKey = "learning.reader.client";
  const client = sessionStorage.getItem(clientKey) || crypto.randomUUID();
  sessionStorage.setItem(clientKey, client);
  const sequence = Number(sessionStorage.getItem("learning.reader.sequence") || 0) + 1;
  sessionStorage.setItem("learning.reader.sequence", String(sequence));
  return { client, sequence, requestId: crypto.randomUUID() };
}

/** Version-bound UI polling only. No model context retrieval or extra page image. */
export class ReadingPreparation {
  private current: PreparationOverview | null = null;
  private timer = 0;
  private disposed = false;
  private visible = true;
  private latest = 0;
  private afterPage = -1;
  private applied = new Map<number, string>();
  private polling = false;
  constructor(
    private endpoint: string,
    private adapter: PdfReaderAdapter,
    private changed: (value: PreparationOverview) => void,
    private failed: (error: unknown) => void,
  ) {}

  setVisible(value: boolean) {
    this.visible = value;
    if (!value) window.clearTimeout(this.timer);
    else void this.poll();
  }

  async focus(value: Schema<"ReadingFocusInput">) {
    this.latest = value.focus_sequence;
    const response = await api.write<PreparationOverview>(
      this.endpoint + "/prepare-current",
      value,
    );
    if (this.disposed || !this.visible || this.latest !== value.focus_sequence) return;
    this.current = response;
    this.changed(response);
    void this.poll();
  }

  private async poll() {
    if (this.disposed || !this.visible || this.polling) return;
    this.polling = true;
    const intent = this.latest;
    try {
      if (this.current) {
        const previous = this.current;
        const prefix = this.endpoint.split("/documents/")[0] + "/preparations/";
        const [current, batch] = await Promise.all([
          api.read<Schema<"PreparationProgress">>(prefix + previous.current.job_id + "/summary"),
          previous.batch && previous.batch.job_id !== previous.current.job_id
            ? api.read<Schema<"PreparationProgress">>(prefix + previous.batch.job_id + "/summary")
            : Promise.resolve(null),
        ]);
        if (this.disposed || !this.visible || this.latest !== intent) return;
        this.current = { ...previous, current, batch: batch || (previous.batch ? current : null) };
        this.changed(this.current);
      }
      const index = await api.read<Schema<"OcrPageIndex">>(
        this.endpoint + "/ocr-index?after_page=" + this.afterPage,
      );
      if (this.disposed || !this.visible) return;
      let fetched = 0;
      for (const page of index.pages) {
        if (this.disposed || !this.visible) return;
        if (this.applied.get(page.page_index) !== page.revision) {
          const layer = await api.read<Schema<"OcrPageLayer">>(
            this.endpoint + "/ocr/" + page.page_index,
          );
          if (this.disposed) return;
          await this.adapter.applyOcr(layer);
          this.applied.set(page.page_index, layer.revision);
          fetched++;
        }
        this.afterPage = page.page_index;
        if (fetched >= 8) break;
      }
      if (this.afterPage === index.pages.at(-1)?.page_index || !index.pages.length)
        this.afterPage = index.next_page ?? -1;
    } catch (error) {
      if (!this.disposed && this.visible) this.failed(error);
      // A read may be repeated; mutations are never retried by this poller.
    } finally {
      this.polling = false;
      if (!this.disposed && this.visible)
        this.timer = window.setTimeout(() => void this.poll(), this.afterPage >= 0 ? 200 : 1500);
    }
  }

  async control(action: "pause" | "resume" | "retry" | "cancel") {
    if (!this.current?.batch) return;
    const job = this.current.batch;
    const prefix = this.endpoint.split("/documents/")[0] + "/preparations/";
    await api.write(prefix + job.job_id + "/" + action, {
      expected_revision: job.revision,
      operation_id: crypto.randomUUID(),
    } satisfies Schema<"PreparationControl">);
    await this.poll();
  }

  dispose() {
    this.disposed = true;
    window.clearTimeout(this.timer);
  }
}

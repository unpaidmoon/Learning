import { api } from "../api/client";
import type { Schema } from "../api/client";
import type { ReadingPosition } from "./actions";
import { PdfReaderAdapter } from "./pdfAdapter";
import { ReadingPreparation, readerIntent } from "./preparation";
import type { PreparationOverview } from "./preparation";

type Intent = ReturnType<typeof readerIntent>;

/** One mounted, version-bound reader. Visibility orders work, not ownership. */
export class ReaderSession {
  readonly adapter: PdfReaderAdapter;
  readonly preparation: ReadingPreparation;
  private abort = new AbortController();
  private state: Schema<"ReadingState"> | null = null;
  private position: ReadingPosition | null = null;
  private pending: ReadingPosition | null = null;
  private openPromise: Promise<void> | null = null;
  private visible = false;
  private disposed = false;
  private ready = false;
  private saving = false;
  private conflicted = false;
  private timer = 0;
  private acquisition = 0;
  private pendingFocus: Intent | null = null;
  private releaseIntent: Intent | null = null;
  private endpoint: string;

  constructor(
    host: HTMLElement,
    file: Schema<"CourseFileOpenResponse">,
    private callbacks: {
      selection(value: Schema<"PdfSelectionInput"> | null): void;
      error(error: unknown): void;
      notice(error: unknown): void;
      preparation(value: PreparationOverview): void;
    },
  ) {
    this.endpoint = `/courses/${file.course_id}/learning/documents/${file.asset_id}/${file.source_version}`;
    this.adapter = new PdfReaderAdapter(host, file, {
      selection: (value) => {
        if (!this.disposed) callbacks.selection(value);
      },
      position: (value) => this.updatePosition(value),
      error: (error) => {
        if (!this.disposed) callbacks.notice(error);
      },
    });
    this.preparation = new ReadingPreparation(
      this.endpoint,
      this.adapter,
      (value) => {
        if (!this.disposed) callbacks.preparation(value);
      },
      (error) => {
        if (!this.disposed) callbacks.notice(error);
      },
    );
    this.preparation.setVisible(false);
  }

  setVisible(value: boolean) {
    if (this.disposed || this.visible === value) return;
    this.visible = value;
    this.preparation.setVisible(value);
    if (!value) {
      this.releaseIntent = this.pendingFocus;
      if (this.state && this.releaseIntent) void this.release(this.state, this.releaseIntent);
      window.clearTimeout(this.timer);
      void this.save();
      return;
    }
    // Allocate both intents at activation, before any network/file read. A
    // delayed open must not mint a newer sequence after a different tab opens.
    const open = readerIntent(),
      focus = readerIntent();
    this.ready = false;
    this.pendingFocus = focus;
    this.acquisition = open.sequence;
    void this.acquire(open, focus);
  }

  private async acquire(open: Intent, focus: Intent) {
    try {
      const [resources, state] = await Promise.all([
        api.read<Schema<"ReadingResourcesView">>("/reading/resources"),
        api.write<Schema<"ReadingState">>(this.endpoint + "/reading-state", {
          reader_client_id: open.client,
          request_id: open.requestId,
          view_sequence: open.sequence,
        } satisfies Schema<"ReadingOpenInput">),
      ]);
      if (this.disposed || !this.visible || this.acquisition !== open.sequence) {
        if (this.releaseIntent && this.releaseIntent.sequence > open.sequence)
          void this.release(state, this.releaseIntent);
        return;
      }
      this.state = state;
      this.conflicted = false;
      if (!resources.prepared)
        throw new Error("PDF 阅读资源尚未准备完成（READING_RESOURCES_NOT_PREPARED）");
      this.openPromise ??= this.adapter.open(resources.pdfjs_version, this.abort.signal, state);
      await this.openPromise;
      if (this.disposed || !this.visible || this.acquisition !== open.sequence) return;
      this.ready = true;
      this.prepare(this.pendingFocus || focus);
    } catch (error) {
      if (!this.disposed && this.visible && this.acquisition === open.sequence)
        this.callbacks.error(error);
    }
  }

  private prepare(intent: Intent) {
    if (!this.state || !this.visible || this.disposed) return;
    void this.preparation
      .focus({
        reader_client_id: intent.client,
        reader_epoch: this.state.reader_epoch,
        focus_sequence: intent.sequence,
        request_id: intent.requestId,
        page_index: this.position?.page_index ?? this.state.page_index,
      })
      .catch((error) => {
        if (
          !this.disposed &&
          this.visible &&
          (error as { code?: string })?.code !== "READING_VIEW_STALE"
        )
          this.callbacks.notice(error);
      });
  }

  private async release(state: Schema<"ReadingState">, intent: Intent) {
    try {
      await api.write(
        this.endpoint + "/focus/release",
        {
          reader_client_id: intent.client,
          reader_epoch: state.reader_epoch,
          focus_sequence: intent.sequence,
          request_id: intent.requestId,
          page_index: this.position?.page_index ?? state.page_index,
        } satisfies Schema<"ReadingFocusInput">,
        { keepalive: true },
      );
    } catch (error) {
      if (!this.disposed && (error as { code?: string })?.code !== "READING_VIEW_STALE")
        this.callbacks.notice(error);
    }
  }

  private updatePosition(value: ReadingPosition) {
    if (!this.visible || this.disposed) return;
    const changedPage = this.position && value.page_index !== this.position.page_index;
    this.position = value;
    this.pending = value;
    if (changedPage) {
      this.pendingFocus = readerIntent();
      if (this.ready) this.prepare(this.pendingFocus);
    }
    window.clearTimeout(this.timer);
    this.timer = window.setTimeout(() => void this.save(), 700);
  }

  private async save() {
    if (!this.state || !this.pending || this.saving || this.conflicted) return;
    const state = this.state,
      position = this.pending;
    this.pending = null;
    this.saving = true;
    try {
      const saved = await api.write<Schema<"ReadingState">>(
        this.endpoint + "/reading-state/save",
        {
          ...position,
          reader_epoch: state.reader_epoch,
          expected_revision: state.revision,
        } satisfies Schema<"ReadingStateUpdate">,
        { keepalive: true },
      );
      if (this.state?.reader_epoch === state.reader_epoch) this.state = saved;
    } catch (error) {
      if (this.state?.reader_epoch === state.reader_epoch) {
        this.conflicted = true;
        if (!this.disposed) this.callbacks.notice(error);
      }
    } finally {
      this.saving = false;
      if (this.pending && !this.conflicted) void this.save();
    }
  }

  dispose() {
    if (this.disposed) return;
    this.setVisible(false);
    this.disposed = true;
    window.clearTimeout(this.timer);
    this.preparation.dispose();
    this.abort.abort();
    this.adapter.dispose();
  }
}

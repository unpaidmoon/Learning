import { api } from "../api/client";
import type { Schema } from "../api/client";
import { readerIntent } from "../reading/preparation";

type Intent = ReturnType<typeof readerIntent>;
type Preparation = Schema<"CurrentPagePreparation">;
export type SubtitleState = {
  preparation: Preparation | null;
  transcript: Schema<"TranscriptView"> | null;
};

/** Local caption display polling; it never adds context to a model request. */
export class SubtitleSession {
  private visible = false;
  private disposed = false;
  private state: SubtitleState = { preparation: null, transcript: null };
  private epoch: string | null = null;
  private position = 0;
  private intent: Intent | null = null;
  private acquisition = 0;
  private timer = 0;
  private polling = false;
  private endpoint: string;
  private prefix: string;
  constructor(
    courseId: string,
    asset: Schema<"MediaAssetResponse">,
    private changed: (state: SubtitleState) => void,
    private failed: (error: unknown) => void,
  ) {
    this.prefix = `/courses/${courseId}/learning`;
    this.endpoint = `${this.prefix}/audio/${asset.asset_id}/${asset.source_version}`;
    this.position = asset.position_ms;
  }

  setVisible(value: boolean) {
    if (this.disposed || this.visible === value) return;
    this.visible = value;
    if (!value) {
      window.clearTimeout(this.timer);
      if (this.epoch && this.intent) void this.release(this.epoch, this.intent);
      return;
    }
    const open = readerIntent();
    this.intent = readerIntent();
    const initialFocus = this.intent;
    this.acquisition = open.sequence;
    this.epoch = null;
    void api
      .write<Schema<"AudioFocusState">>(this.endpoint + "/focus", {
        reader_client_id: open.client,
        request_id: open.requestId,
        view_sequence: open.sequence,
      } satisfies Schema<"ReadingOpenInput">)
      .then(async (state) => {
        if (this.disposed || !this.visible || this.acquisition !== open.sequence) {
          await this.release(state.reader_epoch, initialFocus);
          return;
        }
        this.epoch = state.reader_epoch;
        await this.prepare(this.intent || initialFocus);
        void this.poll();
      })
      .catch((error) => {
        if (this.acquisition === open.sequence) this.error(error);
      });
    void this.poll();
  }

  setPosition(value: number) {
    if (!this.visible || this.disposed || !Number.isFinite(value)) return;
    const position = Math.max(0, Math.round(value));
    const changed = Math.floor(position / 30000) !== Math.floor(this.position / 30000);
    this.position = position;
    if (changed) {
      this.intent = readerIntent();
      if (this.epoch) void this.prepare(this.intent).catch((error) => this.error(error));
    }
  }

  private input(epoch: string, intent: Intent): Schema<"AudioFocusInput"> {
    return {
      reader_client_id: intent.client,
      reader_epoch: epoch,
      request_id: intent.requestId,
      focus_sequence: intent.sequence,
      position_ms: this.position,
    };
  }

  private async prepare(intent: Intent) {
    if (!this.epoch || this.disposed || !this.visible) return;
    const epoch = this.epoch;
    const preparation = await api.write<Preparation>(
      this.endpoint + "/prepare-current",
      this.input(epoch, intent),
    );
    if (
      this.disposed ||
      !this.visible ||
      this.epoch !== epoch ||
      this.intent?.sequence !== intent.sequence
    )
      return;
    this.state = { ...this.state, preparation };
    this.changed(this.state);
    void this.poll();
  }

  private async release(epoch: string, intent: Intent) {
    try {
      await api.write(this.endpoint + "/focus/release", this.input(epoch, intent), {
        keepalive: true,
      });
    } catch (error) {
      if (!this.disposed) this.error(error);
    }
  }

  private error(error: unknown) {
    if (
      !this.disposed &&
      this.visible &&
      (error as { code?: string })?.code !== "READING_VIEW_STALE"
    )
      this.failed(error);
  }

  private async poll() {
    if (this.polling || this.disposed || !this.visible) return;
    this.polling = true;
    const acquisition = this.acquisition;
    try {
      let preparation = this.state.preparation;
      if (preparation) {
        const previous = preparation;
        const base = this.prefix + "/preparations/";
        const [current, batch] = await Promise.all([
          api.read<Schema<"PreparationProgress">>(base + previous.current.job_id + "/summary"),
          previous.batch && previous.batch.job_id !== previous.current.job_id
            ? api.read<Schema<"PreparationProgress">>(base + previous.batch.job_id + "/summary")
            : Promise.resolve(null),
        ]);
        preparation = { ...previous, current, batch: batch || (previous.batch ? current : null) };
      }
      const tracks = await api.read<string[]>(this.endpoint + "/transcripts");
      const unit = Math.max(0, Math.floor(this.position / 30000) - 1);
      const transcript = tracks.length
        ? await api.read<Schema<"TranscriptView">>(
            this.prefix + `/transcripts/${tracks[0]}?first_unit=${unit}&limit=3`,
          )
        : null;
      if (this.disposed || !this.visible || this.acquisition !== acquisition) return;
      // An older polling response cannot undo a newer prepare response.
      if (
        preparation &&
        this.state.preparation &&
        (this.state.preparation.current.job_id !== preparation.current.job_id ||
          this.state.preparation.current.revision > preparation.current.revision)
      )
        preparation = this.state.preparation;
      this.state = { preparation, transcript };
      this.changed(this.state);
    } catch (error) {
      this.error(error);
    } finally {
      this.polling = false;
      if (!this.disposed && this.visible)
        this.timer = window.setTimeout(() => void this.poll(), 1200);
    }
  }

  async control(action: "pause" | "resume" | "retry") {
    const batch = this.state.preparation?.batch;
    if (!batch) return;
    await api.write(this.prefix + `/preparations/${batch.job_id}/${action}`, {
      operation_id: crypto.randomUUID(),
      expected_revision: batch.revision,
    } satisfies Schema<"PreparationControl">);
    await this.poll();
  }

  refresh() {
    void this.poll();
  }

  dispose() {
    if (this.disposed) return;
    this.setVisible(false);
    this.disposed = true;
    window.clearTimeout(this.timer);
  }
}

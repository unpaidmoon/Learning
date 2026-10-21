import { useMemo, useSyncExternalStore } from "react";
import { api, errorMessage } from "../api/client";
import type { Schema } from "../api/client";

export interface DraftReference {
  reference_id: string;
  message_id: string;
  text: string;
  markdown: string;
  source_start?: number;
  source_end?: number;
}

export type Draft = Required<
  Pick<
    Schema<"DraftResponse">,
    "text" | "attachment_ids" | "conditions" | "corrections" | "model" | "thinking"
  >
> & { references: DraftReference[] };

const emptyDraft: Draft = {
  text: "",
  attachment_ids: [],
  references: [],
  conditions: [],
  corrections: [],
  model: null,
  thinking: null,
};
const key = (chatId: string) => `learning.draft.${chatId}`;
type View = { value: Draft; ready: boolean; saving: boolean; error: string; dirty: boolean };

// One browser-side controller per business chat also serializes saves across view switches.
// The database is authoritative; this cache only protects edits during a broken connection.
class DraftController {
  private revision = 0;
  private sequence = 0;
  private savedSequence = 0;
  private saving: Promise<void> | null = null;
  private timer = 0;
  private started = false;
  private listeners = new Set<() => void>();
  private view: View = { value: emptyDraft, ready: false, saving: false, error: "", dirty: false };

  constructor(private chatId: string) {
    try {
      const cached = JSON.parse(localStorage.getItem(key(chatId)) || "null");
      if (cached?.value && typeof cached.revision === "number") {
        this.revision = cached.revision;
        this.view = {
          ...this.view,
          value: { ...emptyDraft, ...cached.value },
          dirty: Boolean(cached.dirty),
        };
      }
    } catch {
      // The server copy remains available if browser storage was cleared or is unavailable.
    }
  }

  subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    if (!this.started) void this.load();
    return () => {
      this.listeners.delete(listener);
    };
  };
  snapshot = () => this.view;
  private emit(patch: Partial<View>) {
    this.view = { ...this.view, ...patch };
    this.listeners.forEach((listener) => listener());
  }
  private cache() {
    try {
      localStorage.setItem(
        key(this.chatId),
        JSON.stringify({ revision: this.revision, value: this.view.value, dirty: this.view.dirty }),
      );
    } catch {
      this.emit({ error: "浏览器本地保护不可用，请保持页面打开并保存草稿。" });
    }
  }
  async load(useServer = false) {
    this.started = true;
    try {
      const remote = await api.read<Schema<"DraftResponse">>(`/chats/${this.chatId}/draft`);
      if (this.view.dirty && !useServer && this.revision !== remote.revision) {
        this.emit({
          error: "草稿有较新的保存版本；本地输入仍保留。请先查看已保存草稿。",
          ready: false,
        });
        return;
      }
      this.revision = remote.revision;
      if (!this.view.dirty || useServer) {
        const { text, attachment_ids, conditions, corrections, model, thinking } = remote;
        const references = (remote as typeof remote & { references?: DraftReference[] }).references;
        this.savedSequence = this.sequence;
        this.emit({
          value: {
            text: text ?? "",
            references: references ?? [],
            attachment_ids: attachment_ids ?? [],
            conditions: conditions ?? [],
            corrections: corrections ?? [],
            model: model ?? null,
            thinking: thinking ?? null,
          },
          dirty: false,
          ready: true,
          error: "",
        });
      } else {
        this.sequence += 1;
        this.emit({ ready: true, error: "" });
        void this.flush().catch(() => undefined);
      }
      this.cache();
    } catch (reason) {
      this.emit({ ready: false, error: `草稿尚未接回：${errorMessage(reason)}` });
    }
  }
  update = (patch: Partial<Draft>) => {
    this.sequence += 1;
    this.emit({ value: { ...this.view.value, ...patch }, dirty: true });
    this.cache();
    window.clearTimeout(this.timer);
    this.timer = window.setTimeout(() => void this.flush().catch(() => undefined), 250);
  };
  flush = (): Promise<void> => {
    window.clearTimeout(this.timer);
    if (this.saving) return this.saving;
    if (!this.view.ready) return Promise.reject(new Error("草稿版本未核对"));
    this.saving = this.saveLoop().finally(() => {
      this.saving = null;
      this.emit({ saving: false });
    });
    return this.saving;
  };
  private async saveLoop() {
    this.emit({ saving: true, error: "" });
    try {
      while (this.savedSequence !== this.sequence || this.view.dirty) {
        const sequence = this.sequence;
        const payload = {
          ...this.view.value,
          expected_revision: this.revision,
        } satisfies Schema<"DraftInput">;
        const result = await api.write<Schema<"DraftResponse">>(
          `/chats/${this.chatId}/draft`,
          payload,
          { keepalive: new TextEncoder().encode(JSON.stringify(payload)).byteLength < 60_000 },
        );
        this.revision = result.revision;
        this.savedSequence = sequence;
        this.emit({ dirty: sequence !== this.sequence });
        this.cache();
      }
    } catch (reason) {
      this.emit({ error: `草稿保存失败，本地输入仍保留：${errorMessage(reason)}` });
      this.cache();
      throw reason;
    }
  }
  clear = async (submissionId: string) => {
    await this.flush();
    const result = await api.write<Schema<"DraftResponse">>(`/chats/${this.chatId}/draft/clear`, {
      expected_revision: this.revision,
      submission_id: submissionId,
    } satisfies Schema<"DraftClearInput">);
    this.revision = result.revision;
    this.savedSequence = ++this.sequence;
    this.emit({
      value: { ...emptyDraft, model: result.model ?? null, thinking: result.thinking ?? null },
      dirty: false,
    });
    this.cache();
  };
}

const controllers = new Map<string, DraftController>();
export async function saveAllDrafts() {
  await Promise.all(
    Array.from(controllers.values())
      .filter((controller) => controller.snapshot().dirty)
      .map((controller) => controller.flush()),
  );
}
export function useDraft(chatId: string) {
  const controller = useMemo(() => {
    if (!controllers.has(chatId)) controllers.set(chatId, new DraftController(chatId));
    return controllers.get(chatId)!;
  }, [chatId]);
  const view = useSyncExternalStore(controller.subscribe, controller.snapshot);
  return { ...view, controller };
}

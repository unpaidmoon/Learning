import { useMemo, useSyncExternalStore } from "react";
import { api, errorMessage } from "../api/client";
import type { Schema } from "../api/client";

export interface DraftReference {
  reference_id: string;
  message_id: string;
  text: string;
  markdown: string;
  source_start?: number | null;
  source_end?: number | null;
}

export type Draft = Required<
  Pick<
    Schema<"DraftResponse">,
    "text" | "attachment_ids" | "conditions" | "corrections" | "model" | "thinking"
  >
> & { references: DraftReference[]; source_snapshot_ids?: string[]; read_course_ids?: string[] };

const emptyDraft: Draft = {
  text: "",
  attachment_ids: [],
  references: [],
  source_snapshot_ids: [],
  read_course_ids: [],
  conditions: [],
  corrections: [],
  model: null,
  thinking: null,
};
const key = (chatId: string) => `learning.draft.${chatId}`;
type View = { value: Draft; ready: boolean; saving: boolean; error: string; dirty: boolean };

/** Preserve legacy unsent content as ordinary text; new edits have no separate fields. */
export function migrateLegacyDraft(value: Draft): Draft {
  const conditions = value.conditions.filter((entry) => entry.trim()).join("\n");
  const corrections = value.corrections.filter((entry) => entry.trim()).join("\n");
  if (!conditions && !corrections) return { ...value, conditions: [], corrections: [] };
  return {
    ...value,
    text: [
      value.text,
      conditions && `补充：\n${conditions}`,
      corrections && `更正：\n${corrections}`,
    ]
      .filter(Boolean)
      .join("\n\n"),
    conditions: [],
    corrections: [],
  };
}

function draftIdentity(value: Draft) {
  return JSON.stringify([
    value.text,
    value.attachment_ids,
    value.source_snapshot_ids || [],
    value.references.map((reference) => [
      reference.reference_id,
      reference.message_id,
      reference.text,
      reference.markdown,
      reference.source_start ?? null,
      reference.source_end ?? null,
    ]),
    value.model ?? null,
    value.thinking ?? null,
    value.read_course_ids || [],
  ]);
}

// One browser-side controller per business chat also serializes saves across view switches.
// The database is authoritative; this cache only protects edits during a broken connection.
class DraftController {
  private revision = 0;
  private sequence = 0;
  private savedSequence = 0;
  private saving: Promise<void> | null = null;
  private timer = 0;
  private started = false;
  private readVersion = 0;
  private listeners = new Set<() => void>();
  private view: View = { value: emptyDraft, ready: false, saving: false, error: "", dirty: false };

  constructor(private chatId: string) {
    try {
      const cached = JSON.parse(localStorage.getItem(key(chatId)) || "null");
      if (cached?.value && typeof cached.revision === "number") {
        this.revision = cached.revision;
        this.view = {
          ...this.view,
          value: migrateLegacyDraft({ ...emptyDraft, ...cached.value }),
          dirty: Boolean(
            cached.dirty || cached.value.conditions?.length || cached.value.corrections?.length,
          ),
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
      this.emit({ error: "浏览器本地输入保护不可用，请保持页面打开，避免丢失未保存输入。" });
    }
  }
  async load(useServer = false, autoSave = true) {
    this.started = true;
    const readVersion = ++this.readVersion;
    try {
      const remote = await api.read<Schema<"DraftResponse">>(`/chats/${this.chatId}/draft`);
      if (readVersion !== this.readVersion) return;
      const incoming = migrateLegacyDraft({ ...emptyDraft, ...remote });
      // A save can commit before the page receives its acknowledgement. Only
      // adopt that newer revision when every authored field agrees exactly.
      const acknowledged =
        this.revision !== remote.revision &&
        draftIdentity(this.view.value) === draftIdentity(incoming);
      if (this.view.dirty && !useServer && this.revision !== remote.revision && !acknowledged) {
        this.emit({
          error: "草稿有较新的保存版本；本地输入仍保留。请先查看已保存草稿。",
          ready: false,
        });
        return incoming;
      }
      this.revision = remote.revision;
      if (!this.view.dirty || useServer || acknowledged) {
        const { text, attachment_ids, conditions, corrections, model, thinking } = remote;
        const references = (remote as typeof remote & { references?: DraftReference[] }).references;
        this.savedSequence = this.sequence;
        const migrating = Boolean(conditions?.length || corrections?.length);
        if (migrating) this.sequence += 1;
        this.emit({
          value: migrateLegacyDraft({
            text: text ?? "",
            references: references ?? [],
            source_snapshot_ids: remote.source_snapshot_ids ?? [],
            read_course_ids: remote.read_course_ids ?? [],
            attachment_ids: attachment_ids ?? [],
            conditions: conditions ?? [],
            corrections: corrections ?? [],
            model: model ?? null,
            thinking: thinking ?? null,
          }),
          dirty: migrating,
          ready: true,
          error: "",
        });
        if (migrating && autoSave) void this.flush().catch(() => undefined);
      } else {
        this.sequence += 1;
        this.emit({ ready: true, error: "" });
        if (autoSave) void this.flush().catch(() => undefined);
      }
      this.cache();
      return incoming;
    } catch (reason) {
      if (readVersion === this.readVersion)
        this.emit({ ready: false, error: `草稿尚未接回：${errorMessage(reason)}` });
    }
  }
  async reconcile() {
    window.clearTimeout(this.timer);
    this.emit({ ready: false });
    await this.saving?.catch(() => undefined);
    return this.load(false, false);
  }
  update = (patch: Partial<Draft>, autoSave = true) => {
    this.sequence += 1;
    this.emit({ value: migrateLegacyDraft({ ...this.view.value, ...patch }), dirty: true });
    this.cache();
    window.clearTimeout(this.timer);
    if (autoSave)
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
      value: {
        ...emptyDraft,
        model: result.model ?? null,
        thinking: result.thinking ?? null,
        read_course_ids: result.read_course_ids ?? [],
      },
      dirty: false,
    });
    this.cache();
  };
}

const controllers = new Map<string, DraftController>();
export function getDraftController(chatId: string) {
  if (!controllers.has(chatId)) controllers.set(chatId, new DraftController(chatId));
  return controllers.get(chatId)!;
}
export async function saveAllDrafts() {
  await Promise.all(
    Array.from(controllers.values())
      .filter((controller) => controller.snapshot().dirty)
      .map((controller) => controller.flush()),
  );
}
export function useDraft(chatId: string) {
  const controller = useMemo(() => getDraftController(chatId), [chatId]);
  const view = useSyncExternalStore(controller.subscribe, controller.snapshot);
  return { ...view, controller };
}

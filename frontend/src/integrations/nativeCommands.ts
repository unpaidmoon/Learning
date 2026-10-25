import { api, ApiError } from "../api/client";
import type { Schema } from "../api/client";
import { INTEGRATIONS_CHANGED } from "./bilibiliUi";

type Session = { sessionId: string };
type Scope = { sessionId?: string; courseId?: string; navigating: boolean };
type Contribution = {
  name: string;
  label: () => string;
  description: () => string;
  available: (session: Session) => boolean;
  ui: { kind: "action"; run: (session: Session) => void };
};
type CommandUi = { register: (entry: Contribution) => () => void; dismiss: (name: string) => void };

/** Adapter to the pinned Harness CommandUiContract, not another slash parser. */
export function installBilibiliCommand({
  commandUi,
  getScope,
  openImport,
  onError,
  events = window,
}: {
  commandUi: CommandUi;
  getScope: () => Scope;
  openImport: (courseId: string) => void;
  onError: (reason: unknown) => void;
  events?: EventTarget;
}) {
  let disposed = false;
  let revision = 0;
  let connection: string | null = null;
  let remove: (() => void) | undefined;
  let pending: AbortController | undefined;

  const clear = () => {
    commandUi.dismiss("bilibili");
    remove?.();
    remove = undefined;
    connection = null;
  };
  const available = (session: Session) => {
    const scope = getScope();
    return (
      !disposed &&
      !!connection &&
      !scope.navigating &&
      !!scope.courseId &&
      scope.sessionId === session.sessionId
    );
  };
  const refresh = async (): Promise<{ connection: string | null; revision: number } | null> => {
    if (disposed) return null;
    const at = ++revision;
    pending?.abort();
    const abort = (pending = new AbortController());
    try {
      const rows = await api.read<Schema<"ModuleView">[]>("/integrations/modules", abort.signal);
      if (disposed || at !== revision) return null;
      const row = rows.find((value) => value.platform === "bilibili");
      const id =
        row?.enabled && row.implemented && ["guest", "connected"].includes(row.connection_state)
          ? row.connection_id
          : null;
      if (id === connection) return { connection, revision: at };
      clear();
      connection = id || null;
      if (!connection) return { connection, revision: at };
      remove = commandUi.register({
        name: "bilibili",
        label: () => "导入B站资料",
        description: () => "浏览收藏夹、读取链接并手选视频或分P，导入当前文件夹",
        available,
        ui: {
          kind: "action",
          run: (session) => {
            const expected = { ...getScope(), connection };
            void (async () => {
              const verified = await refresh();
              const current = getScope();
              if (
                !available(session) ||
                !verified ||
                verified.revision !== revision ||
                current.courseId !== expected.courseId ||
                connection !== expected.connection
              ) {
                throw new ApiError(
                  "BILI_ENTRY_SCOPE_CHANGED",
                  "课程或B站连接已变化，请重新打开入口",
                  409,
                );
              }
              openImport(current.courseId!);
            })().catch((reason) => {
              if (!disposed) onError(reason);
            });
          },
        },
      });
      return { connection, revision: at };
    } catch (reason) {
      if (disposed || at !== revision || abort.signal.aborted) return null;
      clear();
      onError(reason);
      return null;
    }
  };
  const changed = () => {
    void refresh();
  };
  events.addEventListener(INTEGRATIONS_CHANGED, changed);
  events.addEventListener("focus", changed);
  const ready = refresh();
  return {
    ready,
    refresh,
    dispose() {
      disposed = true;
      ++revision;
      pending?.abort();
      clear();
      events.removeEventListener(INTEGRATIONS_CHANGED, changed);
      events.removeEventListener("focus", changed);
    },
  };
}

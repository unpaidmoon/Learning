import { useCallback, useEffect, useRef, useState } from "react";
import { api, errorMessage } from "../api/client";
import type { Schema } from "../api/client";
import { MarkdownAnswer } from "../chat/MarkdownAnswer";
import { Button } from "../components/ui/button";
import { localDraft } from "./model";
import { saveWorkbenchDraft } from "./drafts";
import { QuestionPlayer } from "./QuestionPlayer";
import { useWorkbenchOperation } from "./useWorkbenchOperation";

const kindLabels = { card: "卡片", wrong_question: "错题", knowledge: "知识点" };
type Item = Schema<"ReviewItem">;
type Session = Schema<"ReviewSession">;
type Receipt = Schema<"ReviewRatingReceipt">;

export function ReviewPanel({
  courseId,
  sessionId,
  onTaskCreated,
}: {
  courseId: string;
  sessionId: string;
  onTaskCreated: (task: Schema<"TaskView">) => void;
}) {
  const [today, setToday] = useState<Schema<"TodayReview"> | null>(null);
  const [courseOnly, setCourseOnly] = useState(false);
  const [kind, setKind] = useState("");
  const [management, setManagement] = useState<"" | "active" | "paused" | "removed">("");
  const [managed, setManaged] = useState<Item[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [selected, setSelected] = useState<Item | null>(null);
  const [notice, setNotice] = useState("");
  const requestEpoch = useRef(0);
  const invalidate = useCallback(() => {
    ++requestEpoch.current;
  }, []);
  const refresh = useCallback(async () => {
    const epoch = ++requestEpoch.current;
    const query = new URLSearchParams();
    if (courseOnly) query.set("course_id", courseId);
    if (kind) query.set("kind", kind);
    const nextToday = await api.read<Schema<"TodayReview">>(`/learning/reviews/today?${query}`);
    if (epoch !== requestEpoch.current) return;
    setToday(nextToday);
    if (management) {
      const page = await api.read<Schema<"ReviewItemPage">>(
        `/courses/${courseId}/learning/reviews?state=${management}`,
      );
      if (epoch !== requestEpoch.current) return;
      setManaged(page.items);
      setCursor(page.next_cursor);
    }
  }, [courseOnly, courseId, kind, management]);
  useEffect(() => {
    let live = true;
    queueMicrotask(() => {
      if (live)
        void refresh().catch((error) => {
          if (live) setNotice(errorMessage(error));
        });
    });
    return () => {
      live = false;
      invalidate();
    };
  }, [refresh, invalidate]);
  const rows = management ? managed : (today?.items ?? []);
  return (
    <section aria-label="今日复习" className="today-review">
      <h2>今日复习</h2>
      {today && (
        <p>
          所有课程、类型共用 {today.daily_limit} 项总量。今日已选 {today.admitted_count} 项，已完成{" "}
          {today.completed_count} 项。
        </p>
      )}
      <div className="workbench-toolbar">
        <label>
          课程
          <select
            value={courseOnly ? "current" : "all"}
            onChange={(event) => setCourseOnly(event.target.value === "current")}
          >
            <option value="all">所有课程</option>
            <option value="current">当前课程</option>
          </select>
        </label>
        <label>
          类型
          <select value={kind} onChange={(event) => setKind(event.target.value)}>
            <option value="">全部类型</option>
            {Object.entries(kindLabels).map(([key, label]) => (
              <option key={key} value={key}>
                {label}
              </option>
            ))}
          </select>
        </label>
        <label>
          查看
          <select
            value={management}
            onChange={(event) => {
              setManagement(event.target.value as typeof management);
              setSelected(null);
            }}
          >
            <option value="">今日到期</option>
            <option value="active">当前课程的在队内容</option>
            <option value="paused">当前课程的暂停内容</option>
            <option value="removed">当前课程的已移出内容</option>
          </select>
        </label>
        <Button
          variant="ghost"
          onClick={() => void refresh().catch((error) => setNotice(errorMessage(error)))}
        >
          刷新
        </Button>
      </div>
      <ReviewSettings courseId={courseId} />
      <div className="workbench-columns">
        <nav aria-label="复习列表">
          {rows.map((item) => (
            <button
              key={item.review_id}
              onClick={() => setSelected(item)}
              aria-pressed={selected?.review_id === item.review_id}
            >
              <strong>{item.label || "复习内容"}</strong>
              <small>
                {kindLabels[item.kind]}
                {item.queue_state === "paused"
                  ? " · 已暂停"
                  : item.queue_state === "removed"
                    ? " · 已移出"
                    : ""}
                {!item.eligible ? " · 暂不排期" : ""}
              </small>
            </button>
          ))}
          {management && cursor && (
            <Button
              variant="ghost"
              onClick={() => {
                void api
                  .read<Schema<"ReviewItemPage">>(
                    `/courses/${courseId}/learning/reviews?state=${management}&after_id=${cursor}`,
                  )
                  .then((page) => {
                    setManaged((old) => [...old, ...page.items]);
                    setCursor(page.next_cursor);
                  })
                  .catch((error) => setNotice(errorMessage(error)));
              }}
            >
              更多复习内容
            </Button>
          )}
          {!rows.length && (
            <p>{management ? "此筛选下暂无复习内容。" : "此筛选下暂无今日到期内容。"}</p>
          )}
        </nav>
        <div>
          {selected && (
            <ReviewActivity
              key={selected.review_id}
              initial={selected}
              sessionId={sessionId}
              nativeCourseId={courseId}
              logicalDay={today?.logical_day ?? ""}
              onTaskCreated={onTaskCreated}
              onChanged={() => void refresh().catch((error) => setNotice(errorMessage(error)))}
            />
          )}
        </div>
      </div>
      {notice && <p role="status">{notice}</p>}
    </section>
  );
}

function ReviewSettings({ courseId }: { courseId: string }) {
  const [settings, setSettings] = useState<Schema<"ReviewPreferences"> | null>(null);
  const [limit, setLimit] = useState(20);
  const [notice, setNotice] = useState("");
  const operation = useWorkbenchOperation<Schema<"ReviewPreferences">>(
    courseId + ".review.preferences",
    courseId,
    "practice",
    (value) => {
      setSettings(value);
      setNotice("已保存，从下一复习日应用新的总量。");
    },
  );
  useEffect(() => {
    let live = true;
    void api
      .read<Schema<"ReviewPreferences">>("/learning/reviews/preferences")
      .then((value) => {
        if (live) {
          setSettings(value);
          setLimit(value.daily_limit);
        }
      })
      .catch((error) => {
        if (live) setNotice(errorMessage(error));
      });
    return () => {
      live = false;
    };
  }, []);
  return (
    <details>
      <summary>每日总量</summary>
      <p>所有课程和类型共用；复习日从凌晨4点开始。当天已选内容保留。</p>
      <label>
        每日最多项数
        <input
          type="number"
          min={1}
          max={200}
          value={limit}
          onChange={(event) => setLimit(Number(event.target.value))}
        />
      </label>
      <Button
        disabled={
          !settings || operation.disabled || !Number.isInteger(limit) || limit < 1 || limit > 200
        }
        onClick={() =>
          void operation.execute(`/courses/${courseId}/learning/review-preferences`, {
            expected_revision: settings?.revision,
            daily_limit: limit,
          })
        }
      >
        保存复习总量
      </Button>
      {operation.pending && <Button onClick={() => void operation.reconcile()}>核对原设置</Button>}
      {(notice || operation.notice) && <p role="status">{notice || operation.notice}</p>}
    </details>
  );
}

function ReviewActivity({
  initial,
  sessionId,
  onTaskCreated,
  onChanged,
  nativeCourseId,
  logicalDay,
}: {
  initial: Item;
  sessionId: string;
  onTaskCreated: (task: Schema<"TaskView">) => void;
  onChanged: () => void;
  nativeCourseId: string;
  logicalDay: string;
}) {
  const key = initial.course_id + ".review.activity." + initial.review_id;
  const [saved] = useState(() =>
    localDraft<{ session: Session | null; receipt: Receipt | null }>(key),
  );
  const [item, setItem] = useState(initial);
  const [session, setSession] = useState<Session | null>(saved.value?.session ?? null);
  const [receipt, setReceipt] = useState<Receipt | null>(saved.value?.receipt ?? null);
  const [revision, setRevision] = useState<Schema<"ArtifactRevision"> | null>(null);
  const [notice, setNotice] = useState(saved.error);
  const [nowMs, setNowMs] = useState(Date.now);
  useEffect(() => {
    const timer = setInterval(() => setNowMs(Date.now()), 15000);
    return () => clearInterval(timer);
  }, []);
  const path = `/courses/${item.course_id}/learning/reviews/${item.review_id}`;
  const apply = (value: Item | Session | Receipt) => {
    let currentSession = session,
      currentReceipt = receipt;
    if ("item" in value) {
      setItem(value.item);
      setReceipt(value);
      setSession(value.session);
      currentSession = value.session;
      currentReceipt = value;
    } else if ("session_id" in value) {
      setSession(value);
      currentSession = value;
      if (value.state === "opened") {
        setReceipt(null);
        currentReceipt = null;
      }
    } else {
      setItem(value);
      if (currentSession) {
        void api
          .read<Session>(`${path}/sessions/${currentSession.session_id}`)
          .then((actual) => {
            setSession(actual);
            if (!actual.rating_event_id) setReceipt(null);
            saveWorkbenchDraft(key, {
              session: actual,
              receipt: actual.rating_event_id ? currentReceipt : null,
            });
          })
          .catch((error) => setNotice(errorMessage(error)));
      }
    }
    saveWorkbenchDraft(key, { session: currentSession, receipt: currentReceipt });
    onChanged();
  };
  const operation = useWorkbenchOperation<Item | Session | Receipt>(
    key + ".operation",
    item.course_id,
    "practice",
    apply,
  );
  useEffect(() => {
    let live = true;
    void api
      .read<Item>(path)
      .then((value) => {
        if (live) setItem(value);
      })
      .catch((error) => {
        if (live) setNotice(errorMessage(error));
      });
    return () => {
      live = false;
    };
  }, [path]);
  useEffect(() => {
    let live = true;
    void api
      .read<Schema<"ArtifactRevision">>(
        `/courses/${item.course_id}/learning/artifacts/${item.artifact_id}/versions/${item.content_revision_id}`,
      )
      .then((value) => {
        if (live) setRevision(value);
      })
      .catch((error) => {
        if (live) setNotice(errorMessage(error));
      });
    return () => {
      live = false;
    };
  }, [item.course_id, item.artifact_id, item.content_revision_id]);
  const block = revision?.body.blocks.find((value) => value.block_id === item.block_id);
  const active = item.queue_state === "active" && item.eligible;
  const matching =
    session?.schedule_revision === item.schedule_revision &&
    session?.content_revision_id === item.content_revision_id &&
    session?.logical_day === logicalDay;
  return (
    <article aria-label="复习内容">
      <h3>{kindLabels[item.kind]}</h3>
      {block && (
        <>
          <MarkdownAnswer
            text={block.kind === "path_step" ? block.step.recall_prompt : block.markdown}
          />
          {active &&
            (!session || !matching || session.state === "rated" || session.state === "answered") &&
            item.due_ms <= nowMs && (
              <Button
                disabled={operation.disabled || !!saved.error}
                onClick={() =>
                  void operation.execute(path + "/sessions", {
                    expected_revision: item.schedule_revision,
                  })
                }
              >
                开始本次复习
              </Button>
            )}
          {(block.kind === "card" || block.kind === "path_step") &&
            matching &&
            session?.state !== "opened" && (
              <MarkdownAnswer
                text={block.kind === "card" ? block.card.back : block.step.explanation}
              />
            )}
          {(block.kind === "card" || block.kind === "path_step") &&
            active &&
            matching &&
            session?.state === "opened" && (
              <Button
                disabled={operation.disabled}
                onClick={() =>
                  void operation.execute(`${path}/sessions/${session.session_id}/reveal`, {
                    expected_revision: item.schedule_revision,
                  })
                }
              >
                回忆后翻面
              </Button>
            )}
          {(block.kind === "card" || block.kind === "path_step") &&
            active &&
            matching &&
            session?.state === "revealed" && (
              <div className="workbench-toolbar">
                {["忘记", "困难", "记得", "轻松"].map((label, index) => (
                  <Button
                    key={label}
                    disabled={operation.disabled}
                    onClick={() =>
                      void operation.execute(path + "/rating", {
                        expected_revision: item.schedule_revision,
                        session_id: session.session_id,
                        rating: index + 1,
                      })
                    }
                  >
                    {label}
                  </Button>
                ))}
              </div>
            )}
          {block.kind === "question" && matching && session && (
            <QuestionPlayer
              key={session.session_id}
              courseId={item.course_id}
              artifactId={item.artifact_id}
              revisionId={item.content_revision_id}
              block={block}
              sessionId={item.course_id === nativeCourseId ? sessionId : undefined}
              reviewSession={session}
              onTaskCreated={onTaskCreated}
              onAnswered={() => {
                const next: Session = { ...session, state: "answered" };
                setSession(next);
                saveWorkbenchDraft(key, { session: next, receipt });
                onChanged();
              }}
            />
          )}
        </>
      )}
      {receipt && (
        <>
          <p>已记录本次用户评分，下次到期：{new Date(receipt.item.due_ms).toLocaleString()}</p>
          <Button
            variant="ghost"
            disabled={
              operation.disabled || receipt.item.schedule_revision !== item.schedule_revision
            }
            onClick={() =>
              void operation.execute(path + "/undo", {
                expected_revision: item.schedule_revision,
                event_id: receipt.event_id,
              })
            }
          >
            撤销本次评分
          </Button>
        </>
      )}
      <div className="workbench-toolbar">
        {item.queue_state !== "active" ? (
          <Button
            disabled={operation.disabled}
            onClick={() =>
              void operation.execute(path + "/control", {
                expected_revision: item.schedule_revision,
                action: "resume",
              })
            }
          >
            恢复入队
          </Button>
        ) : (
          <Button
            variant="ghost"
            disabled={operation.disabled}
            onClick={() =>
              void operation.execute(path + "/control", {
                expected_revision: item.schedule_revision,
                action: "pause",
              })
            }
          >
            暂停复习
          </Button>
        )}
        {item.queue_state !== "removed" && (
          <Button
            variant="ghost"
            disabled={operation.disabled}
            onClick={() =>
              void operation.execute(path + "/control", {
                expected_revision: item.schedule_revision,
                action: "remove",
              })
            }
          >
            移出复习
          </Button>
        )}
      </div>
      {operation.pending && (
        <Button disabled={operation.busy} onClick={() => void operation.reconcile()}>
          核对原复习操作
        </Button>
      )}
      {(notice || operation.notice) && <p role="status">{notice || operation.notice}</p>}
    </article>
  );
}

import { useEffect, useState } from "react";
import { api, errorMessage } from "../api/client";
import type { Schema } from "../api/client";
import { Button } from "../components/ui/button";
import { SourcePreview } from "./SourcePreview";
import type { Task } from "./model";

export function ResearchPanel({ courseId, task }: { courseId: string; task: Task }) {
  const [value, setValue] = useState<Schema<"ResearchView"> | null>(null);
  const [evidence, setEvidence] = useState<Schema<"ResearchEvidencePage"> | null>(null);
  const [notice, setNotice] = useState("");
  const base = `/courses/${courseId}/learning/tasks/${task.task_id}/research`;
  const reads = value?.external_reads ?? [];
  useEffect(() => {
    let live = true;
    const refresh = () =>
      void api
        .read<Schema<"ResearchView">>(base)
        .then((v) => {
          if (live) setValue(v);
        })
        .catch((reason) => {
          if (live) setNotice(errorMessage(reason));
        });
    refresh();
    const timer = ["queued", "running", "unknown"].includes(task.state)
      ? setInterval(refresh, 2500)
      : null;
    return () => {
      live = false;
      if (timer) clearInterval(timer);
    };
  }, [base, task.revision, task.state]);
  return (
    <section className="workbench-coverage" aria-label="研究主题与证据">
      <h3>研究主题与证据</h3>
      {value?.checkpoints.map((item) => (
        <p key={item.unit_id}>
          {item.title} · {item.phase === "synthesis" ? "综合归纳" : "证据整理"} ·{" "}
          {{
            completed: "已保存",
            pending: "待开始",
            read: "已准备",
            dispatching: "进行中",
            failed: "未完成",
            unknown: "待对账",
          }[item.state] ?? item.state}
          {item.evidence_count > 0 && (
            <Button
              variant="ghost"
              onClick={() => {
                void api
                  .read<Schema<"ResearchEvidencePage">>(`${base}/evidence/${item.unit_id}`)
                  .then(setEvidence)
                  .catch((reason) => setNotice(errorMessage(reason)));
              }}
            >
              查看依据{item.evidence_from_prior_runs > 0 ? "（沿用已读版本）" : ""}
            </Button>
          )}
        </p>
      ))}
      {!!value?.reconciliation_required.length && <p>原执行仍需对账，不会另建研究请求。</p>}
      {!!reads.length && (
        <details>
          <summary>外部读取记录</summary>
          {reads.map((read) => (
            <p key={read.effect_id}>
              {read.query ?? read.url} ·{" "}
              {{
                completed: "已取得文字",
                failed: "未取得证据",
                started: "读取中或待对账",
                cancelled: "已停止",
              }[read.state] ?? read.state}
              {read.last_http_status !== null ? ` · HTTP ${read.last_http_status}` : ""}
              {read.transport_source === "fixture" ? " · 本地验证响应" : ""}
              {read.reused_evidence ? " · 沿用已保存版本" : ""}
              {read.error_code ? ` · ${read.error_code}` : ""}
            </p>
          ))}
          <p>外部HTTP读取与模型出站分别记账，缺失的外部费用字段为未知。</p>
        </details>
      )}
      {evidence && (
        <aside aria-label="研究依据快照">
          {evidence.items.map((item) => (
            <article key={item.source.snapshot_id}>
              {!item.from_current_run && <p>此版本已在本任务先前步骤读取，本轮沿用其证据引用。</p>}
              <SourcePreview source={item.source} />
            </article>
          ))}
          <Button variant="ghost" onClick={() => setEvidence(null)}>
            关闭研究依据
          </Button>
        </aside>
      )}
      {notice && <p role="status">{notice}</p>}
    </section>
  );
}

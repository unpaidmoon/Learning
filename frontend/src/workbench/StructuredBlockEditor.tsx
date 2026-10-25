import type { Schema } from "../api/client";

type Block = Schema<"ArtifactView">["revision"]["body"]["blocks"][number];

export function StructuredBlockEditor({
  block,
  disabled,
  onChange,
}: {
  block: Block;
  disabled: boolean;
  onChange: (block: Block) => void;
}) {
  if (block.kind === "visualization") {
    const envelope = block.visualization;
    const data = envelope.payload.data;
    const changeData = (changed: typeof data) =>
      onChange({
        ...block,
        visualization: { ...envelope, payload: { ...envelope.payload, data: changed } },
      });
    return (
      <div>
        <label>
          图形标题
          <input
            disabled={disabled}
            value={envelope.presentation?.title ?? ""}
            onChange={(event) =>
              onChange({
                ...block,
                visualization: {
                  ...envelope,
                  presentation: {
                    description: "",
                    alt_text: "",
                    aspect_ratio: "4:3",
                    ...envelope.presentation,
                    title: event.target.value,
                  },
                },
              })
            }
          />
        </label>
        {data.kind === "cartesian" && (
          <>
            <label>
              函数表达式
              <input
                disabled={disabled}
                value={data.expression}
                onChange={(event) =>
                  changeData({ ...data, expression: event.target.value, expression_tree: null })
                }
              />
            </label>
            <p>用x和下列参数表示函数，支持四则运算、^、sin、cos、tan、exp、log、sqrt和abs。</p>
            {(["x_range", "y_range"] as const).map((axis) => (
              <fieldset key={axis} disabled={disabled}>
                <legend>{axis === "x_range" ? "横轴范围" : "纵轴范围"}</legend>
                {(["min", "max"] as const).map((end) => (
                  <label key={end}>
                    {end === "min" ? "下界" : "上界"}
                    <input
                      type="number"
                      value={data[axis][end]}
                      onChange={(event) =>
                        changeData({
                          ...data,
                          [axis]: { ...data[axis], [end]: Number(event.target.value) },
                        })
                      }
                    />
                  </label>
                ))}
              </fieldset>
            ))}
            {(data.parameters ?? []).map((parameter, index) => (
              <fieldset key={parameter.symbol} disabled={disabled}>
                <legend>
                  {parameter.label}（{parameter.symbol}）
                </legend>
                {(["min", "max", "step", "initial"] as const).map((field) => (
                  <label key={field}>
                    {{ min: "最小值", max: "最大值", step: "步长", initial: "初值" }[field]}
                    <input
                      type="number"
                      value={parameter[field]}
                      onChange={(event) =>
                        changeData({
                          ...data,
                          parameters: (data.parameters ?? []).map((p, i) =>
                            i === index ? { ...p, [field]: Number(event.target.value) } : p,
                          ),
                        })
                      }
                    />
                  </label>
                ))}
              </fieldset>
            ))}
          </>
        )}
        {data.kind === "diagram" &&
          data.nodes.map((node, index) => (
            <fieldset key={node.node_id} disabled={disabled}>
              <legend>{node.label || "图形节点"}</legend>
              <label>
                节点文字
                <input
                  value={node.label ?? ""}
                  onChange={(event) =>
                    changeData({
                      ...data,
                      nodes: data.nodes.map((n, i) =>
                        i === index ? { ...n, label: event.target.value } : n,
                      ),
                    })
                  }
                />
              </label>
              {(["x", "y"] as const).map((axis) => (
                <label key={axis}>
                  {axis === "x" ? "水平位置（%）" : "垂直位置（%）"}
                  <input
                    type="number"
                    min={0}
                    max={100}
                    value={node[axis]}
                    onChange={(event) =>
                      changeData({
                        ...data,
                        nodes: data.nodes.map((n, i) =>
                          i === index ? { ...n, [axis]: Number(event.target.value) } : n,
                        ),
                      })
                    }
                  />
                </label>
              ))}
            </fieldset>
          ))}
      </div>
    );
  }
  if (block.kind === "path_step") {
    const step = block.step;
    return (
      <div>
        {(
          [
            ["title", "知识点名称"],
            ["objective", "学习目标"],
            ["recall_prompt", "知识点回忆问题"],
            ["explanation", "知识点讲解"],
          ] as const
        ).map(([field, label]) => (
          <label key={field}>
            {label}
            <textarea
              disabled={disabled}
              value={step[field]}
              onChange={(event) =>
                onChange({ ...block, step: { ...step, [field]: event.target.value } })
              }
            />
          </label>
        ))}
        <label>
          建议顺序
          <input
            type="number"
            min={0}
            max={1000000}
            disabled={disabled}
            value={step.suggested_order ?? 0}
            onChange={(event) =>
              onChange({
                ...block,
                step: { ...step, suggested_order: Number(event.target.value) },
              })
            }
          />
        </label>
      </div>
    );
  }
  if (block.kind === "essay_draft") {
    const rubric = block.essay.rubric ?? {
      max_total: 100,
      aggregation: "independent" as const,
      dimensions: [],
      description: "",
    };
    return (
      <>
        <label>
          作文题目与要求
          <textarea
            disabled={disabled}
            value={block.essay.topic}
            onChange={(event) =>
              onChange({ ...block, essay: { ...block.essay, topic: event.target.value } })
            }
          />
        </label>
        <label>
          作文评分标准
          <textarea
            disabled={disabled}
            value={rubric.description}
            onChange={(event) =>
              onChange({
                ...block,
                essay: { ...block.essay, rubric: { ...rubric, description: event.target.value } },
              })
            }
          />
        </label>
        <label>
          作文参考满分
          <input
            type="number"
            disabled={disabled || rubric.aggregation === "sum"}
            value={rubric.max_total}
            onChange={(event) =>
              onChange({
                ...block,
                essay: {
                  ...block.essay,
                  rubric: { ...rubric, max_total: Number(event.target.value) },
                },
              })
            }
          />
        </label>
      </>
    );
  }
  if (block.kind === "card")
    return (
      <label>
        卡片背面
        <textarea
          aria-label="卡片背面"
          disabled={disabled}
          value={block.card.back}
          onChange={(event) => onChange({ ...block, card: { back: event.target.value } })}
        />
      </label>
    );
  if (block.kind !== "question") return null;
  const content = block.question;
  return (
    <div className="practice-content-editor">
      <label>
        题型
        <select
          disabled={disabled}
          value={content.question_type}
          onChange={(event) =>
            onChange({
              ...block,
              question: {
                ...content,
                question_type: event.target.value as typeof content.question_type,
              },
            })
          }
        >
          {Object.entries({
            single_choice: "单选",
            multiple_choice: "多选",
            true_false: "判断",
            short_answer: "简答",
            essay: "论述",
            calculation: "计算",
            proof: "证明",
            numeric: "数值",
            ordering: "排序",
            matching: "匹配",
          }).map(([key, label]) => (
            <option key={key} value={key}>
              {label}
            </option>
          ))}
        </select>
      </label>
      {(content.options ?? []).map((option, index) => (
        <label key={option.option_id}>
          选项 {option.option_id}
          <textarea
            disabled={disabled}
            value={option.text}
            onChange={(event) =>
              onChange({
                ...block,
                question: {
                  ...content,
                  options: (content.options ?? []).map((item, ordinal) =>
                    ordinal === index ? { ...item, text: event.target.value } : item,
                  ),
                },
              })
            }
          />
        </label>
      ))}
      <label>
        参考答案
        <textarea
          aria-label="参考答案"
          disabled={disabled}
          value={content.reference_answer}
          onChange={(event) =>
            onChange({
              ...block,
              question: {
                ...content,
                reference_answer: event.target.value,
                answer_authority: event.target.value.trim() ? "ai_reference" : "missing",
              },
            })
          }
        />
      </label>
      <label>
        解析
        <textarea
          disabled={disabled}
          value={content.explanation}
          onChange={(event) =>
            onChange({ ...block, question: { ...content, explanation: event.target.value } })
          }
        />
      </label>
      <label>
        <input
          type="checkbox"
          disabled={disabled || !content.reference_answer.trim()}
          checked={content.answer_authority === "user_confirmed"}
          onChange={(event) =>
            onChange({
              ...block,
              question: {
                ...content,
                answer_authority: event.target.checked
                  ? "user_confirmed"
                  : content.reference_answer.trim()
                    ? "ai_reference"
                    : "missing",
              },
            })
          }
        />
        我已核对这个参考答案
      </label>
    </div>
  );
}

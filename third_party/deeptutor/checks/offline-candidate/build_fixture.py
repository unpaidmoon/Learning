"""Build a synthetic offline book through actual retained DeepTutor models/code."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import inspect
import json
from pathlib import Path
import shutil
import sys
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from deeptutor_reuse.book_models import (
    Block as LearningBlock,
    BlockStatus,
    BlockType,
    Book,
    BookStatus,
    Chapter,
    Page as BookPage,
    Progress,
    SourceAnchor,
    Spine,
)
from deeptutor_reuse.book_compiler_state import finalize_terminal_page
from deeptutor_reuse.visualization_models import (
    RendererRef,
    VisualizationEnvelope,
    VisualizationPayload,
)

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[3]
UPSTREAM = ROOT / "upstream"
PACKAGE = ROOT / "package"
TOOLS = ROOT / "tools"
EVIDENCE = ROOT / "evidence"
COMMIT = "a31d82624c27d75f4e771d4fcb78dce16470b383"


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def import_file(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class StrictData(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Inline(StrictData):
    kind: Literal["text", "math"]
    value: str = Field(min_length=1, max_length=3000)

    @model_validator(mode="after")
    def safe_math(self):
        if self.kind == "math" and any(
            token in self.value.lower()
            for token in ["\\href", "\\url", "\\includegraphics", "\\html"]
        ):
            raise ValueError("MATH_EXTERNAL_OR_HTML_COMMAND")
        return self


class TextPayload(StrictData):
    body: str
    paragraphs: list[list[Inline]]
    display_math: list[str] = Field(default_factory=list)


class FigurePayload(StrictData):
    description: str
    asset_path: str
    asset_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    alt: str
    caption: str

    @field_validator("asset_path")
    @classmethod
    def local_asset(cls, value):
        if (
            not value.startswith("assets/")
            or ":" in value
            or "\\" in value
            or ".." in Path(value).parts
            or value.startswith("//")
        ):
            raise ValueError("ASSET_OUTSIDE_PACKAGE")
        return value


class InteractivePayload(StrictData):
    description: str
    visualization_id: str


class RangeParameter(StrictData):
    label: str
    min: float
    max: float
    step: float = Field(gt=0)
    initial: float

    @model_validator(mode="after")
    def valid_range(self):
        if not self.min < self.max or not self.min <= self.initial <= self.max:
            raise ValueError("PARAMETER_RANGE_INVALID")
        if (self.max - self.min) / self.step > 10000:
            raise ValueError("PARAMETER_RANGE_TOO_LARGE")
        return self


class PlotData(StrictData):
    kind: Literal["quadratic_tangent"]
    a: RangeParameter
    x0: RangeParameter


class Question(StrictData):
    question_id: str
    question_type: Literal["multiple_choice", "written"]
    question: str
    options: dict[str, str] | None = None
    correct_answer: str
    explanation: str

    @model_validator(mode="after")
    def independent_contract(self):
        if self.question_type == "multiple_choice" and (
            not self.options or len(self.options) < 2
        ):
            raise ValueError("CHOICE_OPTIONS_MISSING")
        return self


class QuizPayload(StrictData):
    questions: list[Question] = Field(min_length=1, max_length=20)


class FlashCard(StrictData):
    front: str
    back: str


class FlashCardsPayload(StrictData):
    cards: list[FlashCard] = Field(min_length=1, max_length=20)


class CalloutPayload(StrictData):
    label: str
    body: str


PAYLOAD_TYPES = {
    BlockType.TEXT: TextPayload,
    BlockType.FIGURE: FigurePayload,
    BlockType.INTERACTIVE: InteractivePayload,
    BlockType.QUIZ: QuizPayload,
    BlockType.FLASH_CARDS: FlashCardsPayload,
    BlockType.CALLOUT: CalloutPayload,
}


def prepare_retained_modules():
    TOOLS.mkdir(exist_ok=True)
    # Original imports are the sole adaptations; unrelated registry/model paths
    # are excluded from the selected prefix rather than stubbed at runtime.
    base_source = (UPSTREAM / "deeptutor/book/blocks/base.py").read_text("utf-8")
    base_prefix = base_source.split("\nclass BlockGeneratorRegistry:", 1)[0]
    base_prefix = base_prefix.replace(
        "from ..models import (", "from deeptutor_reuse.book_models import ("
    )
    (TOOLS / "compiler_core.py").write_text(base_prefix, encoding="utf-8", newline="\n")
    for old, new in [
        ("export.py", "book_export.py"),
        ("progress.py", "progress_core.py"),
    ]:
        source = (UPSTREAM / "deeptutor/book" / old).read_text("utf-8")
        source = source.replace(
            "from .models import ", "from deeptutor_reuse.book_models import "
        )
        (TOOLS / new).write_text(source, encoding="utf-8", newline="\n")
    return (
        import_file("offline_dt_compiler_core", TOOLS / "compiler_core.py"),
        import_file("offline_dt_export", TOOLS / "book_export.py"),
        import_file("offline_dt_progress", TOOLS / "progress_core.py"),
    )


def image_fixture():
    points = []
    for i in range(101):
        x = -2.2 + i * 4.4 / 100
        points.append(f"{320 + x * 100:.2f},{390 - x * x * 65:.2f}")
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="640" height="430" viewBox="0 0 640 430">'
        '<rect width="640" height="430" fill="#f6f8fc"/>'
        '<path d="M50 390H600M320 410V25" fill="none" stroke="#8793a7" stroke-width="1.5"/>'
        '<path d="M420 325L520 130" fill="none" stroke="#d5913b" stroke-width="2" stroke-dasharray="7 6"/>'
        f'<polyline points="{" ".join(points)}" fill="none" stroke="#315fb1" stroke-width="4"/>'
        '<circle cx="420" cy="325" r="6" fill="#315fb1"/><circle cx="520" cy="130" r="6" fill="#315fb1"/>'
        '<text x="433" y="349" font-family="sans-serif" font-size="17" fill="#1f2f4b">A (1, 1)</text>'
        '<text x="530" y="129" font-family="sans-serif" font-size="17" fill="#1f2f4b">B (2, 4)</text>'
        '<text x="80" y="48" font-family="sans-serif" font-size="20" fill="#315fb1">y = x²</text>'
        '<text x="594" y="414" font-family="sans-serif" font-size="16" fill="#4b5c73">x</text></svg>'
    ).encode("utf-8")


async def build():
    PACKAGE.mkdir(exist_ok=True)
    EVIDENCE.mkdir(exist_ok=True)
    for sub in ["assets", "sources", "licenses", "runtime", "vendor/katex"]:
        (PACKAGE / sub).mkdir(parents=True, exist_ok=True)
    compiler, exporter, progress_code = prepare_retained_modules()
    expected_models = REPO / "third_party/deeptutor/upstream/deeptutor/book/models.py"
    active_models = Path(inspect.getsourcefile(BookPage))
    assert active_models.read_bytes() == expected_models.read_bytes(), (
        "Original Book model bytes changed"
    )
    katex_dir = REPO / "frontend/node_modules/katex"
    assert (
        json.loads((katex_dir / "package.json").read_text("utf-8"))["version"]
        == "0.16.22"
    )
    for name in ["katex.min.js", "katex.min.css"]:
        shutil.copyfile(katex_dir / "dist" / name, PACKAGE / "vendor/katex" / name)
    for font in (katex_dir / "dist/fonts").iterdir():
        if font.is_file():
            target = PACKAGE / "vendor/katex/fonts" / font.name
            target.parent.mkdir(exist_ok=True)
            shutil.copyfile(font, target)
    shutil.copyfile(katex_dir / "LICENSE", PACKAGE / "licenses/KaTeX-LICENSE")
    shutil.copyfile(
        REPO / "third_party/deeptutor/LICENSE", PACKAGE / "licenses/DeepTutor-LICENSE"
    )
    svg = image_fixture()
    (PACKAGE / "assets/parabola-secant.svg").write_bytes(svg)
    source_text = "合成材料（非用户资料）\n定义：二次函数 f(x)=ax^2，其中 a>0。\n导数：f'(x)=2ax。切点 x0 处斜率为 2ax0。\n例：a=1，x0=2 时，切线斜率为4。\n"
    source_bytes = source_text.encode("utf-8")
    (PACKAGE / "sources/fixture-source.txt").write_bytes(source_bytes)
    source_version = sha(source_bytes)
    anchor = SourceAnchor(
        kind="manual", ref="fixture-source:lines-2-4", snippet="f(x)=ax^2；f'(x)=2ax。"
    )
    plot = PlotData(
        kind="quadratic_tangent",
        a=RangeParameter(label="系数 a", min=0.5, max=3, step=0.5, initial=1),
        x0=RangeParameter(label="切点 x₀", min=-2, max=2, step=0.25, initial=1),
    )
    envelope = VisualizationEnvelope(
        render_type="quadratic_tangent",
        renderer=RendererRef(
            id="offline.function-plot",
            version="1.0.0",
            target="native",
            native_renderer="quadratic_tangent",
        ),
        payload=VisualizationPayload(
            format="learning.parameter-plot/v1", data=plot.model_dump()
        ),
    )
    data = [
        (
            "b-text",
            BlockType.TEXT,
            "从图像理解瞬时变化",
            "source_content",
            {
                "body": "设 $f(x)=ax^2$，其中 $a>0$。切线斜率为 $f'(x_0)=2ax_0$。",
                "paragraphs": [
                    [
                        {
                            "kind": "text",
                            "value": "从最简单的二次函数出发，看看曲线怎样随参数变化。设 ",
                        },
                        {"kind": "math", "value": "f(x)=ax^2"},
                        {"kind": "text", "value": "，其中 "},
                        {"kind": "math", "value": "a>0"},
                        {"kind": "text", "value": "。"},
                    ],
                    [
                        {
                            "kind": "text",
                            "value": "曲线上一点附近的变化率，由导数刻画。切线斜率由同一条公式确定；改变参数后，图像和数值应一起更新。",
                        }
                    ],
                ],
                "display_math": [r"f'(x)=2ax", r"k=f'(x_0)=2ax_0"],
            },
        ),
        (
            "b-figure",
            BlockType.FIGURE,
            "先看两个点之间的割线",
            "ai_supplement",
            {
                "description": "合成抛物线与两个点，用于辅助解释，非原材料图片。",
                "asset_path": "assets/parabola-secant.svg",
                "asset_sha256": sha(svg),
                "alt": "合成图：y等于x平方，标注A点1、1和B点2、4。",
                "caption": "两点割线只是平均变化率。下方参数图展示切点处的瞬时变化率。",
            },
        ),
        (
            "b-plot",
            BlockType.INTERACTIVE,
            "动手改变系数与切点",
            "ai_supplement",
            {
                "description": "仅由有界数值参数绘图；所有计算在当前文件内完成。",
                "visualization_id": "viz-quadratic",
            },
        ),
        (
            "b-quiz",
            BlockType.QUIZ,
            "做两道小练习",
            "synthetic_example",
            {
                "questions": [
                    {
                        "question_id": "q-choice",
                        "question_type": "multiple_choice",
                        "question": "当 a=1、x₀=2 时，切线斜率是多少？",
                        "options": {"A": "2", "B": "4", "C": "8"},
                        "correct_answer": "4",
                        "explanation": "代入 k=2ax₀，得到 2×1×2=4。",
                    },
                    {
                        "question_id": "q-written",
                        "question_type": "written",
                        "question": "为什么“读过解析”不能直接说明已经掌握？请用自己的话说明。",
                        "correct_answer": "读过只是接触了信息，能独立说明和应用还需要另行检验。",
                        "explanation": "这是主观表达题。离线包只提供参考，揭示答案不产生对错或掌握判定。",
                    },
                ]
            },
        ),
        (
            "b-cards",
            BlockType.FLASH_CARDS,
            "记住关键关系",
            "synthetic_example",
            {
                "cards": [
                    {"front": "二次函数 ax² 的导数是什么？", "back": "2ax。"},
                    {
                        "front": "切点移动时，切线斜率由什么决定？",
                        "back": "由系数 a 和切点 x₀ 共同决定：k=2ax₀。",
                    },
                ]
            },
        ),
        (
            "b-note",
            BlockType.CALLOUT,
            "继续学习",
            "ai_supplement",
            {
                "label": "练习建议",
                "body": "回到参数图，先预测斜率，再拖动参数核对。这里只保留本次打开期间的自测状态，不会回传正式学习记录。",
            },
        ),
    ]

    class FixtureGenerator(compiler.BlockGenerator):
        async def _generate(self, ctx):
            assert ctx.block.status == BlockStatus.GENERATING
            if ctx.block.params.get("fixture_fail"):
                raise compiler.GenerationFailure("Fixture payload is empty")
            if ctx.block.params.get("fixture_cancel"):
                raise asyncio.CancelledError()
            payload = (
                PAYLOAD_TYPES[ctx.block.type]
                .model_validate(ctx.block.params["fixture_payload"])
                .model_dump()
            )
            return (
                payload,
                ctx.block.source_anchors,
                {"compiled_from_pure_fixture": True},
            )

    book = Book(
        id="fixture-book",
        title="二次函数：从图像到切线",
        description="完全合成的离线阅读、参数交互与自测包",
        language="zh",
        status=BookStatus.READY,
        revision=1,
        created_at=0,
        updated_at=0,
        page_count=2,
        chapter_count=2,
    )
    chapters = [
        Chapter(
            id="chapter-graph", title="从图像到切线", page_ids=["page-graph"], order=0
        ),
        Chapter(
            id="chapter-practice",
            title="自测与核对",
            page_ids=["page-practice"],
            order=1,
        ),
    ]
    spine = Spine(book_id=book.id, chapters=chapters, updated_at=0)
    pages = [
        BookPage(
            id="page-graph",
            book_id=book.id,
            chapter_id=chapters[0].id,
            title=chapters[0].title,
            order=0,
            created_at=0,
            updated_at=0,
        ),
        BookPage(
            id="page-practice",
            book_id=book.id,
            chapter_id=chapters[1].id,
            title=chapters[1].title,
            order=1,
            created_at=0,
            updated_at=0,
        ),
    ]
    generator = FixtureGenerator()
    for index, (identifier, kind, title, role, payload) in enumerate(data):
        block = LearningBlock(
            id=identifier,
            type=kind,
            title=title,
            source_anchors=[anchor],
            params={"fixture_payload": payload},
            metadata={
                "content_role": role,
                "source_ids": ["fixture-source"],
                "source_version": source_version,
            },
            created_at=0,
            updated_at=0,
        )
        page = pages[0] if index < 3 else pages[1]
        ctx = compiler.BlockContext(
            book_id=book.id,
            chapter=chapters[0 if index < 3 else 1],
            page=page,
            block=block,
            language="zh",
            rag_enabled=False,
        )
        compiled = await generator.generate(ctx)
        assert compiled.status == BlockStatus.READY and compiled.payload
        page.blocks.append(compiled)
    for page in pages:
        finalize_terminal_page(page)
        assert page.status.value == "ready"
    assert (
        BookPage.model_validate_json(pages[0].model_dump_json()).blocks[2].type
        == BlockType.INTERACTIVE
    )
    negative = []
    for label, constructor, values in [
        (
            "external_image",
            FigurePayload,
            {
                "description": "bad",
                "asset_path": "https://invalid.example/x.svg",
                "asset_sha256": "a" * 64,
                "alt": "bad",
                "caption": "bad",
            },
        ),
        (
            "traversal_image",
            FigurePayload,
            {
                "description": "bad",
                "asset_path": "assets/../private.svg",
                "asset_sha256": "a" * 64,
                "alt": "bad",
                "caption": "bad",
            },
        ),
        (
            "html_math",
            Inline,
            {"kind": "math", "value": r"\href{https://invalid.example}{x}"},
        ),
        ("code_expression", PlotData, {**plot.model_dump(), "expression": "eval(1)"}),
    ]:
        try:
            constructor.model_validate(values)
        except ValidationError:
            negative.append({"fixture": label, "status": "rejected"})
        else:
            raise AssertionError(label)
    failed = LearningBlock(
        id="failed-fixture", type=BlockType.TEXT, params={"fixture_fail": True}
    )
    failed_ctx = compiler.BlockContext(
        book_id=book.id,
        chapter=chapters[0],
        page=pages[0],
        block=failed,
        rag_enabled=False,
    )
    assert (await generator.generate(failed_ctx)).status == BlockStatus.ERROR
    cancelled = LearningBlock(
        id="cancelled-fixture", type=BlockType.TEXT, params={"fixture_cancel": True}
    )
    try:
        await generator.generate(
            compiler.BlockContext(
                book_id=book.id,
                chapter=chapters[0],
                page=pages[0],
                block=cancelled,
                rag_enabled=False,
            )
        )
    except asyncio.CancelledError:
        negative.append(
            {
                "fixture": "compiler_cancel",
                "status": "propagated",
                "upstream_block_state": cancelled.status.value,
            }
        )
    else:
        raise AssertionError("Cancellation swallowed")
    progress = Progress(book_id=book.id)
    progress_code.record_attempt(
        progress,
        page_id=pages[1].id,
        block_id="b-quiz",
        question_id="q-written",
        user_answer="revealed",
        is_correct=None,
        page_to_chapter={pages[1].id: chapters[1].id},
        submission_id="fixture-reveal",
    )
    assert progress.score == 0 and progress.weak_chapters == []
    payload = {
        "schema_version": "learning.offline-candidate/v1",
        "book": book.model_dump(mode="json"),
        "spine": spine.model_dump(mode="json"),
        "pages": [page.model_dump(mode="json") for page in pages],
        "visualizations": {"viz-quadratic": envelope.model_dump(mode="json")},
        "source_catalog": [
            {
                "id": "fixture-source",
                "title": "二次函数合成材料",
                "source_version": source_version,
                "path": "sources/fixture-source.txt",
                "text": source_text,
                "origin": "synthetic_fixture",
            }
        ],
    }
    encoded = json.dumps(payload, ensure_ascii=False, indent=2)
    (PACKAGE / "book-data.json").write_text(
        encoded + "\n", encoding="utf-8", newline="\n"
    )
    markdown = exporter.render_book_markdown(book, spine, pages)
    assert "切线斜率" in markdown and "做两道小练习" in markdown
    (PACKAGE / "book.md").write_text(markdown, encoding="utf-8", newline="\n")
    html = (ROOT / "templates/index.template.html").read_text("utf-8")
    html = html.replace(
        "__BOOK_DATA__", encoded.replace("<", "\\u003c").replace("&", "\\u0026")
    )
    (PACKAGE / "index.html").write_text(html, encoding="utf-8", newline="\n")
    for name in ["reader.js", "reader.css"]:
        shutil.copyfile(ROOT / "templates" / name, PACKAGE / "runtime" / name)
    evidence = {
        "source_commit": COMMIT,
        "active_original_model_sha256": sha(active_models.read_bytes()),
        "original_model_identity_passed": True,
        "compiled_block_count": 6,
        "page_statuses": [page.status.value for page in pages],
        "actual_model_requests": 0,
        "original_compiler_success_error_cancel_used": True,
        "negative_fixtures": negative,
        "ungraded_not_wrong_passed": True,
        "retained_export_module_used": True,
        "katex_version": "0.16.22",
        "source_fixture_version": source_version,
    }
    (EVIDENCE / "python-model-compile.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(evidence, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(build())

"""Two synthetic chapters through the actual independent package; no provider."""

import asyncio
import json

from deeptutor_reuse.book_block_compiler import BlockGenerator
from deeptutor_reuse.book_models import (
    Block as LearningBlock,
    BlockType,
    Book,
    BookInputs,
    Chapter,
    Page as BookPage,
    Spine,
)
from deeptutor_reuse.book_resume import (
    BookCompileSnapshot,
    begin_block,
    complete_block,
    confirm_outline,
    make_block_context,
    next_block,
    outline_digest,
    readable_chapters,
)


class SyntheticText(BlockGenerator):
    block_type = BlockType.TEXT

    async def _generate(self, context):
        return (
            {"body": context.block.params["text"]},
            [],
            {"content_role": "synthetic_example"},
        )


async def main():
    book = Book(id="demo", title="合成函数小书", page_count=2, chapter_count=2)
    chapters = [
        Chapter(id=f"c{i}", title=f"第{i}章", page_ids=[f"p{i}"], order=i)
        for i in (1, 2)
    ]
    pages = [
        BookPage(
            id=f"p{i}",
            book_id=book.id,
            chapter_id=f"c{i}",
            title=f"第{i}章",
            blocks=[
                LearningBlock(
                    id=f"b{i}", type=BlockType.TEXT, params={"text": f"合成内容{i}"}
                )
            ],
        )
        for i in (1, 2)
    ]
    state = BookCompileSnapshot(
        book=book,
        spine=Spine(book_id=book.id, chapters=chapters),
        pages=pages,
        inputs=BookInputs(
            source_refs=[{"asset_id": "synthetic", "source_version": "v1"}]
        ),
    )
    state = confirm_outline(
        state, outline_digest(state)
    )  # Only from the application's explicit confirmation.
    ready_during_compile = []
    while (item := next_block(state)) is not None:
        started = begin_block(state, item, f"demo:{item.block_id}")
        state = (
            started.snapshot
        )  # Application atomically persists this before dispatch.
        if started.dispatch:
            receipt = await SyntheticText().generate(make_block_context(state, item))
            state = complete_block(state, f"demo:{item.block_id}", receipt)
            ready_during_compile.append(readable_chapters(state))
            # Application atomically persists this completed state and receipt.
    print(
        json.dumps(
            {
                "status": state.book.status.value,
                "confirmation_count": state.confirmation_count,
                "readable_after_each_commit": ready_during_compile,
                "model_calls": 0,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())

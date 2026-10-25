"""Authenticated outline confirmation and independent, versioned book reading."""

import asyncio

from fastapi import FastAPI

from learning_app.errors import AppError

from .book_models import BookDirectoryUpdate, BookOutlineConfirm, BookOutlineView, BookView
from .books import BookStore
from .models import ArtifactReceipt, TaskView


def install_book_routes(app: FastAPI):
    prefix = "/api/v1/courses/{course_id}/learning"

    @app.get(prefix + "/tasks/{task_id}/book-outline", response_model=BookOutlineView)
    def book_outline(course_id: str, task_id: str):
        return BookStore(app.state.repository).outline(course_id, task_id)

    @app.post(prefix + "/tasks/{task_id}/book-outline", response_model=TaskView)
    async def confirm_book_outline(course_id: str, task_id: str, value: BookOutlineConfirm):
        async with app.state.submission_gate:
            if app.state.exit_prepared:
                raise AppError("APPLICATION_EXIT_PREPARED", "后台正在退出，未确认新的目录生成")
            result = await asyncio.to_thread(
                BookStore(app.state.repository).confirm, course_id, task_id, value
            )
        app.state.task_executor.schedule(course_id, task_id)
        return result

    @app.get(prefix + "/artifacts/{artifact_id}/book", response_model=BookView)
    def book(course_id: str, artifact_id: str, revision_id: str | None = None):
        return BookStore(app.state.repository).get(course_id, artifact_id, revision_id)

    @app.post(prefix + "/artifacts/{artifact_id}/book-directory", response_model=ArtifactReceipt)
    def book_directory(course_id: str, artifact_id: str, value: BookDirectoryUpdate):
        return BookStore(app.state.repository).update_directory(course_id, artifact_id, value)

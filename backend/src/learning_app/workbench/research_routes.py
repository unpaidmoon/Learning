"""Read-only research progress for its original course and task."""

from fastapi import FastAPI

from .research import ResearchStore
from .research_models import ResearchEvidencePage, ResearchView


def install_research_routes(app: FastAPI):
    @app.get(
        "/api/v1/courses/{course_id}/learning/tasks/{task_id}/research", response_model=ResearchView
    )
    def research(course_id: str, task_id: str):
        return ResearchStore(app.state.repository).get(course_id, task_id)

    @app.get(
        "/api/v1/courses/{course_id}/learning/tasks/{task_id}/research/evidence/{unit_id}",
        response_model=ResearchEvidencePage,
    )
    def evidence(course_id: str, task_id: str, unit_id: str):
        return ResearchEvidencePage(
            items=ResearchStore(app.state.repository).evidence(course_id, task_id, unit_id)
        )

"""User corpus policy/status; no provider or model tool owns exclusions."""

from fastapi import Query

from .corpus import CourseCorpus
from .corpus_models import CorpusPolicy, CorpusPolicyReceipt, CorpusStatus
from .evidence import ChatEvidence, chat_evidence


def install_corpus_routes(app):
    prefix = "/api/v1/courses/{course_id}/learning/corpus"

    @app.get(prefix, response_model=CorpusStatus)
    def status(course_id: str, after: str = Query("", max_length=1000)):
        return CourseCorpus(app.state.repository).status(course_id, after=after)

    @app.post(prefix + "/policy", response_model=CorpusPolicyReceipt)
    def configure(course_id: str, value: CorpusPolicy):
        result = CourseCorpus(app.state.repository).configure(course_id, value)
        app.state.preparations.wake()
        return result

    @app.get("/api/v1/desktop/sessions/{session_id}/learning-evidence", response_model=ChatEvidence)
    def evidence(session_id: str, after: str = Query("", max_length=160)):
        binding = app.state.desktop_gateway.mapping(session_id)
        return chat_evidence(
            app.state.repository, binding["course_id"], binding["chat_id"], after=after
        )

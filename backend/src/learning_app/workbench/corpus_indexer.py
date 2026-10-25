"""Background document steps share the existing one-worker local queue."""

import logging

from sqlalchemy import select, update

from learning_app.errors import AppError

from . import corpus_schema as c
from .common import now
from .corpus import CourseCorpus
from .preparations import PreparationStore
from .processing_models import PreparationCreate

logger = logging.getLogger(__name__)


class CorpusIndexer:
    def __init__(self, repository, project_root):
        self.repository, self.project_root = repository, project_root
        self._next_course, self._discovery = 0, True

    def step(self, cancelled):
        courses = self.repository.list_courses()
        if not courses:
            return False
        course = courses[self._next_course % len(courses)]["course_id"]
        self._next_course += 1
        corpus = CourseCorpus(self.repository)
        try:
            if self._discovery:
                return corpus.discover(course, cancelled=cancelled)
            changed = corpus.index_next(course, cancelled=cancelled)
            self._queue_ocr(course)
            return changed
        except AppError as error:
            if error.code == "PREPARATION_INTERRUPTED":
                raise
            if error.code not in {"COURSE_ROOT_REQUIRED", "COURSE_NOT_FOUND"}:
                self._scan_error(course, error.code)
        except OSError:
            self._scan_error(course, "CORPUS_SOURCE_UNAVAILABLE")
        finally:
            # Change phase after each complete course round, so one course is
            # not permanently assigned discovery while another only indexes.
            if self._next_course % len(courses) == 0:
                self._discovery = not self._discovery
        return False

    def _scan_error(self, course, code):
        with self.repository.engine.begin() as conn:
            conn.execute(
                update(c.scans)
                .where(c.scans.c.course_id == course)
                .values(state="failed", error_code=code, updated_at=now())
            )
        logger.warning("Course corpus step unavailable: %s", code)

    def _queue_ocr(self, course):
        with self.repository.engine.connect() as conn:
            docs = (
                conn.execute(
                    select(c.documents).where(
                        c.documents.c.course_id == course,
                        c.documents.c.current.is_(True),
                        c.documents.c.state == "needs_ocr",
                    )
                )
                .mappings()
                .all()
            )
            profile = CourseCorpus.profile(conn, course)
        from .corpus import excluded

        for doc in docs:
            if excluded(doc["relative_path"], profile["exclusions"]):
                continue
            try:
                # Full scope with no focus priority remains background work.
                # Existing durable paused/failed jobs are returned, not resumed.
                PreparationStore(self.repository).create_pdf(
                    course,
                    PreparationCreate(
                        kind="pdf_ocr",
                        asset_id=doc["asset_id"],
                        source_version=doc["source_version"],
                    ),
                    self.project_root,
                )
            except AppError as error:
                with self.repository.engine.begin() as conn:
                    conn.execute(
                        update(c.documents)
                        .where(c.documents.c.document_id == doc["document_id"])
                        .values(error_code=error.code)
                    )
            break

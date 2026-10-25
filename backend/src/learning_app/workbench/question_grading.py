"""Original DeepStudent deterministic graders behind a versioned, local helper."""

from .models import QuestionBlock
from .scheduler import CalculationHelper


class QuestionGrader:
    def __init__(self):
        self.helper = CalculationHelper("question")

    def grade(self, block: QuestionBlock, actual: str, request_id: str, revision_id: str):
        question = block.question
        return self.helper.invoke(
            request_id,
            fields={
                "question": {
                    "question_id": block.block_id,
                    "question_revision_id": revision_id,
                    "source_snapshot_ids": block.source_snapshot_ids,
                    "question_type": question.question_type,
                    "reference_status": {
                        "user_confirmed": "confirmed",
                        "ai_reference": "ai_reference",
                        "missing": "unavailable",
                    }[question.answer_authority],
                    "answer": question.reference_answer or None,
                    "structured_data": None,
                },
                "user_answer": actual,
            },
        )

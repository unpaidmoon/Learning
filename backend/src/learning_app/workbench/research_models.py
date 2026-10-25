"""Task-specific research scope and read-only durable checkpoint projections."""

from pydantic import Field, model_validator

from .models import Contract, Identity, SourceSnapshot


class ResearchScope(Contract):
    topics: list[str] = Field(default_factory=list, max_length=24)
    urls: list[str] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def exact_topics(self):
        from deeptutor_reuse.research_records import ResearchTopicView, TopicBlock

        view = ResearchTopicView(())
        for index, title in enumerate(self.topics):
            if not title.strip() or len(title) > 1000 or view.has_topic(title):
                raise ValueError("research topics must be nonempty and distinct")
            view = ResearchTopicView((*view.blocks, TopicBlock(str(index), title.strip(), "")))
        from .external_http import public_url

        self.urls = [public_url(url) for url in self.urls]
        if len(self.urls) != len(set(self.urls)):
            raise ValueError("duplicate research URL")
        return self


class ResearchEvidence(Contract):
    source: SourceSnapshot
    read_in_run_id: Identity
    from_current_run: bool


class ResearchCheckpoint(Contract):
    unit_id: Identity
    topic_id: Identity
    title: str
    state: str
    phase: str
    run_id: Identity | None
    error_code: str | None
    evidence_count: int
    evidence_from_prior_runs: int = 0


class ResearchEvidencePage(Contract):
    items: list[ResearchEvidence]


class ResearchExternalReadView(Contract):
    effect_id: Identity
    run_id: Identity
    kind: str
    state: str
    query: str | None
    url: str | None
    transport_source: str
    requests_started: int
    responses_observed: int
    last_http_status: int | None
    error_code: str | None
    created_at: str
    finished_at: str | None
    reused_evidence: bool = False


class ResearchView(Contract):
    task_id: Identity
    task_revision: int
    allow_external: bool
    topics: list[str]
    checkpoints: list[ResearchCheckpoint]
    statistics: dict[str, int]
    reconciliation_required: list[Identity]
    source_decision_required: list[Identity]
    external_reads: list[ResearchExternalReadView] = Field(default_factory=list)

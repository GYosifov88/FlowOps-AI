"""Incident analysis (V2-M3): build the context, ask the analyzer, verify, store.

* NEVER automatic: the only callers are POST /api/incidents/{id}/analyze and the opt-in eval
  script. Ingesting events, syncing or viewing a page never reaches this module.
* The context is exactly `build_context` (V2-M2). Its hash is stored with every row.
* Rows are only ever INSERTED. A repeat is a new row; nothing is updated or deleted.
* Dedup: without `force`, an existing OK row for the same incident, context hash, model and
  prompt version is returned and the analyzer is not called (no cost). Failed attempts
  (refused, incomplete, invalid, error) never count, so a retry is always possible. Two
  simultaneous requests can both call the model; that is accepted (rare, costs a fraction of a cent).
* The model call takes 10-60 s. The session holds no write transaction while it runs.
"""
import json
import logging
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analysis.analyzer import Analyzer, AnalyzerResult
from app.analysis.prompt import PROMPT_VERSION
from app.analysis.verify import verify_analysis
from app.models import AnalysisStatus, Incident, IncidentAnalysis
from app.services.incident_context import build_context, context_hash
from app.services.incidents import IncidentNotFound

logger = logging.getLogger(__name__)


@dataclass
class AnalysisOutcome:
    result: Literal["created", "reused"]
    row: IncidentAnalysis


def _existing_ok(db: Session, incident_id: int, ctx_hash: str, analyzer: Analyzer) -> IncidentAnalysis | None:
    return db.scalar(
        select(IncidentAnalysis)
        .where(
            IncidentAnalysis.incident_id == incident_id,
            IncidentAnalysis.context_hash == ctx_hash,
            IncidentAnalysis.model == analyzer.model,
            IncidentAnalysis.prompt_version == PROMPT_VERSION,
            IncidentAnalysis.status == AnalysisStatus.OK,
        )
        .order_by(IncidentAnalysis.id.desc())
        .limit(1)
    )


def _call(analyzer: Analyzer, context: dict) -> AnalyzerResult:
    try:
        return analyzer.analyze(context)
    except Exception as exc:  # the contract says "never raises"; a bug must not lose the attempt
        logger.error("analyzer raised %s", type(exc).__name__)
        return AnalyzerResult("error", error_type=type(exc).__name__[:100])


def analyze_incident(
    db: Session, incident_id: int, analyzer: Analyzer, *, force: bool = False
) -> AnalysisOutcome:
    """Raises IncidentNotFound. Returns the stored row (new, or the reused one)."""
    context = build_context(db, incident_id)
    ctx_hash = context_hash(context)

    if not force:
        existing = _existing_ok(db, incident_id, ctx_hash, analyzer)
        if existing is not None:
            logger.info("analysis reused incident=%s analysis=%s", incident_id, existing.id)
            return AnalysisOutcome("reused", existing)

    db.rollback()  # end the read transaction: nothing is held open during the slow call
    result = _call(analyzer, context)

    analysis_json = notes_json = None
    if result.status == "ok" and result.output is not None:
        verified = verify_analysis(result.output, context)
        analysis_json = verified.analysis.model_dump_json()
        notes_json = json.dumps(verified.notes)

    row = IncidentAnalysis(
        incident_id=incident_id,
        context_hash=ctx_hash,
        context_version=context["context_version"],
        prompt_version=PROMPT_VERSION,
        provider=analyzer.provider,
        model=analyzer.model,
        status=AnalysisStatus(result.status),
        analysis_json=analysis_json,
        validation_notes_json=notes_json,
        refusal=result.refusal,
        error_type=result.error_type,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        reasoning_tokens=result.reasoning_tokens,
        forced=force,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    # Metadata only. Never the context, the prompt or the model's output.
    logger.info(
        "analysis stored incident=%s analysis=%s model=%s status=%s in=%s out=%s reasoning=%s",
        incident_id, row.id, row.model, row.status.value,
        row.input_tokens, row.output_tokens, row.reasoning_tokens,
    )
    return AnalysisOutcome("created", row)


def list_analyses(db: Session, incident_id: int) -> list[IncidentAnalysis]:
    """Newest first. Raises IncidentNotFound."""
    if db.get(Incident, incident_id) is None:
        raise IncidentNotFound
    return list(
        db.scalars(
            select(IncidentAnalysis)
            .where(IncidentAnalysis.incident_id == incident_id)
            .order_by(IncidentAnalysis.id.desc())
        )
    )

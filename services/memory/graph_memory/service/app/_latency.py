"""Latency statistics endpoint for LLM call telemetry.

Provides percentile-based timeout recommendations from llm_call_log data.
Keeps all AQL in the memory project — agents call /latency-stats, not raw AQL.

Usage:
    POST /latency-stats
    {
        "model": "deepseek-ai/DeepSeek-V3",  # optional filter
        "provider": "chutes",                 # optional filter
        "days": 7,                            # lookback window (default 7)
        "status": "ok",                       # only count successful calls (default)
        "prompt_tokens": 2000,                # estimate for similar-sized requests
        "completion_tokens": 500              # expected output size
    }

Response:
    {
        "model": "deepseek-ai/DeepSeek-V3",
        "provider": "chutes",
        "sample_count": 142,
        "percentiles": {
            "p50": 2100,
            "p75": 3400,
            "p90": 5200,
            "p95": 8100,
            "p99": 12400
        },
        "tokens_per_second": {
            "p50": 45.2,
            "p95": 22.1
        },
        "estimated_timeout_ms": 28500,       # based on token counts if provided
        "recommended_timeout_ms": 8100,      # p95 from similar requests
        "date_range": {"from": "2026-04-05", "to": "2026-04-12"}
    }

Token-aware estimation:
    When prompt_tokens and/or completion_tokens are provided, the endpoint finds
    similar requests (within ±50% token range) and calculates tokens-per-second
    throughput to give a more accurate timeout for your specific request size.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, HTTPException
from loguru import logger
from pydantic import BaseModel, Field

router = APIRouter(tags=["latency"])

_COLLECTION = "llm_call_log"


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class LatencyStatsRequest(BaseModel):
    model: str | None = None  # filter by model_served
    provider: str | None = None  # filter by provider
    days: int = Field(default=7, ge=1, le=90)  # lookback window
    status: str = "ok"  # filter by status (ok/error/all)
    prompt_tokens: int | None = None  # for token-aware estimation
    completion_tokens: int | None = None  # expected output tokens


class LatencyPercentiles(BaseModel):
    p50: int
    p75: int
    p90: int
    p95: int
    p99: int


class ThroughputStats(BaseModel):
    """Tokens per second throughput statistics."""
    p50_tps: float  # median throughput
    p95_tps: float  # conservative throughput for timeout estimation


class DateRange(BaseModel):
    from_date: str = Field(alias="from")
    to_date: str = Field(alias="to")

    class Config:
        populate_by_name = True


class LatencyStatsResponse(BaseModel):
    model: str | None
    provider: str | None
    sample_count: int
    percentiles: LatencyPercentiles | None
    throughput: ThroughputStats | None = None  # tokens/second stats
    estimated_timeout_ms: int | None = None  # based on token counts if provided
    recommended_timeout_ms: int
    date_range: DateRange


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


@router.post("/latency-stats")
async def get_latency_stats(body: LatencyStatsRequest) -> LatencyStatsResponse:
    """Calculate latency percentiles from llm_call_log for timeout estimation.

    Filters by model, provider, and date range. Returns p50/p75/p90/p95/p99
    with the p95 as the recommended timeout (safe default for most use cases).
    """
    try:
        from ...arango_client import get_db
        db = get_db()
    except Exception as exc:
        logger.error("latency-stats: DB connection failed: {}", exc)
        raise HTTPException(status_code=503, detail=f"Database unavailable: {exc}") from exc

    # Build date range
    now = datetime.now(timezone.utc)
    from_date = (now - timedelta(days=body.days)).strftime("%Y-%m-%d")
    to_date = now.strftime("%Y-%m-%d")

    # Build AQL filter
    filters = ["doc.duration_ms != null"]
    bind_vars: dict[str, Any] = {"from_date": from_date}

    filters.append("doc.date >= @from_date")

    if body.model:
        filters.append("doc.model_served == @model")
        bind_vars["model"] = body.model

    if body.provider:
        filters.append("doc.provider == @provider")
        bind_vars["provider"] = body.provider

    if body.status and body.status != "all":
        filters.append("doc.status == @status")
        bind_vars["status"] = body.status

    filter_clause = " AND ".join(filters)

    # Query for durations + token counts (we need both for throughput calculation)
    query = f"""
        FOR doc IN {_COLLECTION}
        FILTER {filter_clause}
        SORT doc.duration_ms ASC
        RETURN {{
            duration_ms: doc.duration_ms,
            total_tokens: doc.total_tokens,
            prompt_tokens: doc.prompt_tokens,
            completion_tokens: doc.completion_tokens
        }}
    """

    try:
        cursor = db.aql.execute(query, bind_vars=bind_vars)
        results = list(cursor)
    except Exception as exc:
        logger.error("latency-stats: AQL query failed: {}", exc)
        raise HTTPException(status_code=500, detail=f"Query failed: {exc}") from exc

    sample_count = len(results)

    if sample_count == 0:
        # No data — return fallback timeout
        return LatencyStatsResponse(
            model=body.model,
            provider=body.provider,
            sample_count=0,
            percentiles=None,
            recommended_timeout_ms=30000,  # 30s fallback
            date_range=DateRange(**{"from": from_date, "to": to_date}),
        )

    durations = [r["duration_ms"] for r in results]

    # Calculate percentiles
    def percentile(data: list[int], p: float) -> int:
        idx = int(len(data) * p)
        idx = min(idx, len(data) - 1)
        return int(data[idx])

    percentiles = LatencyPercentiles(
        p50=percentile(durations, 0.50),
        p75=percentile(durations, 0.75),
        p90=percentile(durations, 0.90),
        p95=percentile(durations, 0.95),
        p99=percentile(durations, 0.99),
    )

    # Calculate throughput (tokens per second)
    throughput = None
    estimated_timeout_ms = None
    tps_values = []

    for r in results:
        tokens = r.get("total_tokens") or 0
        duration_ms = r.get("duration_ms") or 0
        if tokens > 0 and duration_ms > 0:
            tps = (tokens / duration_ms) * 1000  # tokens per second
            tps_values.append(tps)

    if tps_values:
        tps_values.sort()
        p50_tps = tps_values[int(len(tps_values) * 0.50)]
        # p95 TPS = 5th percentile (slowest) for conservative estimate
        p95_tps = tps_values[int(len(tps_values) * 0.05)] if len(tps_values) >= 20 else tps_values[0]
        throughput = ThroughputStats(
            p50_tps=round(p50_tps, 2),
            p95_tps=round(p95_tps, 2),
        )

        # Estimate timeout based on expected token count
        if body.prompt_tokens or body.completion_tokens:
            expected_tokens = (body.prompt_tokens or 0) + (body.completion_tokens or 0)
            if p95_tps > 0:
                estimated_timeout_ms = int((expected_tokens / p95_tps) * 1000 * 1.2)  # 20% buffer

    return LatencyStatsResponse(
        model=body.model,
        provider=body.provider,
        sample_count=sample_count,
        percentiles=percentiles,
        throughput=throughput,
        estimated_timeout_ms=estimated_timeout_ms,
        recommended_timeout_ms=estimated_timeout_ms or percentiles.p95,
        date_range=DateRange(**{"from": from_date, "to": to_date}),
    )


# ---------------------------------------------------------------------------
# Batch endpoint for multiple models
# ---------------------------------------------------------------------------


class BatchLatencyRequest(BaseModel):
    models: list[str] | None = None  # if None, return all models
    days: int = Field(default=7, ge=1, le=90)


class ModelLatency(BaseModel):
    model: str
    provider: str
    sample_count: int
    p95_ms: int
    recommended_timeout_ms: int


class BatchLatencyResponse(BaseModel):
    models: list[ModelLatency]
    date_range: DateRange


@router.post("/latency-stats/batch")
async def get_batch_latency_stats(body: BatchLatencyRequest) -> BatchLatencyResponse:
    """Get latency stats for multiple models in one call.

    If models is None, returns stats for all models with data in the period.
    """
    try:
        from ...arango_client import get_db
        db = get_db()
    except Exception as exc:
        logger.error("latency-stats/batch: DB connection failed: {}", exc)
        raise HTTPException(status_code=503, detail=f"Database unavailable: {exc}") from exc

    now = datetime.now(timezone.utc)
    from_date = (now - timedelta(days=body.days)).strftime("%Y-%m-%d")
    to_date = now.strftime("%Y-%m-%d")

    # AQL to aggregate by model with percentile calculation
    model_filter = ""
    bind_vars: dict[str, Any] = {"from_date": from_date}

    if body.models:
        model_filter = "FILTER doc.model_served IN @models"
        bind_vars["models"] = body.models

    query = f"""
        FOR doc IN {_COLLECTION}
        FILTER doc.date >= @from_date
        FILTER doc.duration_ms != null
        FILTER doc.status == "ok"
        {model_filter}
        COLLECT model = doc.model_served, provider = doc.provider
        INTO durations = doc.duration_ms
        LET sorted_durations = SORTED(durations)
        LET count = LENGTH(sorted_durations)
        LET p95_idx = FLOOR(count * 0.95)
        LET p95 = count > 0 ? sorted_durations[MIN([p95_idx, count - 1])] : 30000
        RETURN {{
            model: model,
            provider: provider,
            sample_count: count,
            p95_ms: p95
        }}
    """

    try:
        cursor = db.aql.execute(query, bind_vars=bind_vars)
        results = list(cursor)
    except Exception as exc:
        logger.error("latency-stats/batch: AQL query failed: {}", exc)
        raise HTTPException(status_code=500, detail=f"Query failed: {exc}") from exc

    models = [
        ModelLatency(
            model=r["model"] or "unknown",
            provider=r["provider"] or "unknown",
            sample_count=r["sample_count"],
            p95_ms=int(r["p95_ms"]) if r["p95_ms"] else 30000,
            recommended_timeout_ms=int(r["p95_ms"]) if r["p95_ms"] else 30000,
        )
        for r in results
    ]

    return BatchLatencyResponse(
        models=models,
        date_range=DateRange(**{"from": from_date, "to": to_date}),
    )

"""Analytics pipeline endpoint: recall → describe → chart data.

Inputs:
    POST /analytics/run with JSON body:
        q (str):     Natural language query to analyze.
        type (str):  Visualization type — "describe", "chart", or "figure".
        scope (str): Collection scope filter (default "operational").
        limit (int): Max recall results (default 20, capped at 100).

Outputs:
    On success: {status, description, chart_type, chart_data, record_count, degraded?}
    On no data: {status: "no_data", description: "No matching records found"}
    On error:   HTTP 400/500 with detail message.

Failure modes:
    - Empty query → 400.
    - Recall raises ConnectionError/OSError → 503 (database unavailable).
    - Recall returns zero results → 200 with status "no_data".
    - Chart extraction yields no labels → response omits chart_data, includes degraded.
"""
from __future__ import annotations

import asyncio
from collections import Counter
from functools import partial
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from loguru import logger
from pydantic import BaseModel, Field

from ...api import MemoryClient

router = APIRouter(prefix="/analytics", tags=["analytics"])
_client = MemoryClient()

# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

_MAX_LIMIT = 100


class AnalyticsRequest(BaseModel):
    q: str
    type: str = "describe"  # describe, chart, figure
    scope: str = "operational"
    limit: int = Field(default=20, ge=1, le=_MAX_LIMIT)


class ChartData(BaseModel):
    type: Literal["bar", "pie", "line", "hbar", "donut", "area"]
    title: str
    labels: list[str]
    values: list[float]
    series: list[dict[str, Any]] | None = None
    colors: list[str] | None = None


class AnalyticsDegradation(BaseModel):
    """Explains WHY the requested visualization could not be rendered."""
    requested_type: str | None = None
    actual_type: str
    reason: str
    data_available: str
    suggestion: str


class AnalyticsResponse(BaseModel):
    status: str
    description: str
    chart_type: str | None = None
    chart_data: ChartData | None = None
    record_count: int = 0
    degraded: AnalyticsDegradation | None = None


# ---------------------------------------------------------------------------
# Colorblind-safe palette (Tableau 10 inspired)
# ---------------------------------------------------------------------------

_CHART_COLORS = [
    "#4e79a7", "#f28e2b", "#59a14f", "#e15759", "#76b7b2",
    "#edc948", "#b07aa1", "#9c755f", "#bab0ac", "#ff9da7",
]

_CATEGORICAL_TYPES = {"bar", "hbar", "pie", "donut", "grouped_bar", "stacked_bar", "treemap"}
_TEMPORAL_TYPES = {"line", "multi_line", "area", "stacked_area"}
_MATRIX_TYPES = {"heatmap", "scatter", "bubble"}
_SUPPORTED_TYPES = {"bar", "pie", "line", "hbar", "donut", "area"}


# ---------------------------------------------------------------------------
# Data-shape detection
# ---------------------------------------------------------------------------


def _detect_data_shape(results: list[dict[str, Any]]) -> str:
    """Classify recalled results into: time_series, multivariate, categorical, minimal, empty."""
    if not results:
        return "empty"

    has_dates = any(
        any(k in r for k in ("date", "timestamp", "created_at", "time"))
        for r in results[:10]
    )
    sample = results[0]
    numeric_fields = [
        k for k, v in sample.items()
        if isinstance(v, (int, float)) and k not in ("_key",)
    ]
    scopes = Counter(r.get("scope", "unknown") for r in results)

    if has_dates and len(results) >= 3:
        return "time_series"
    if len(numeric_fields) >= 2:
        return "multivariate"
    if len(scopes) >= 2:
        return "categorical"
    if len(results) >= 5:
        return "categorical"
    return "minimal"


def _chart_fits_data(chart_type: str, data_shape: str) -> bool:
    if data_shape == "empty":
        return False
    if data_shape == "minimal":
        return chart_type in ("bar", "hbar", "pie", "donut", "text", "table")
    if chart_type in _CATEGORICAL_TYPES:
        return data_shape in ("categorical", "multivariate", "minimal")
    if chart_type in _TEMPORAL_TYPES:
        return data_shape == "time_series"
    if chart_type in _MATRIX_TYPES:
        return data_shape in ("multivariate", "categorical")
    return True


def _best_fallback_type(data_shape: str) -> str:
    return {
        "time_series": "line",
        "multivariate": "bar",
        "categorical": "bar",
        "minimal": "bar",
        "empty": "bar",
    }.get(data_shape, "bar")


# ---------------------------------------------------------------------------
# Field helpers
# ---------------------------------------------------------------------------


def _find_numeric_field(results: list[dict[str, Any]]) -> str | None:
    if not results:
        return None
    skip = {"_key", "_id", "_rev"}
    for key, val in results[0].items():
        if key not in skip and isinstance(val, (int, float)):
            return key
    return None


# ---------------------------------------------------------------------------
# Data extraction by shape
# ---------------------------------------------------------------------------


def _extract_time_series(results: list[dict[str, Any]]) -> tuple[list[str], list[float]]:
    time_field = None
    for key in ("date", "timestamp", "created_at", "time"):
        if any(key in r for r in results[:5]):
            time_field = key
            break
    if not time_field:
        return _extract_categorical(results, "")

    value_field = _find_numeric_field(results)
    sorted_results = sorted(results, key=lambda r: str(r.get(time_field, "")))
    if value_field:
        buckets: dict[str, float] = {}
        for r in sorted_results:
            date_key = str(r.get(time_field, ""))[:10]
            buckets[date_key] = buckets.get(date_key, 0) + float(r.get(value_field, 0))
        return list(buckets.keys()), list(buckets.values())

    counts = Counter(str(r.get(time_field, ""))[:10] for r in sorted_results)
    return list(counts.keys()), [float(v) for v in counts.values()]


def _extract_categorical(results: list[dict[str, Any]], query: str) -> tuple[list[str], list[float]]:
    """Smart categorical grouping: try tags, scope, bridge_attributes, then fallback."""
    q_lower = query.lower()

    if any(kw in q_lower for kw in ("persona", "scope", "by scope", "who")):
        counts = Counter(r.get("scope", "unknown") for r in results)
        return list(counts.keys()), [float(v) for v in counts.values()]

    # Try tag-based grouping first
    from ...lessons.store import get_mind_tags
    all_tags: list[str] = []
    for r in results:
        tags = r.get("tags") or get_mind_tags(r) or []
        if isinstance(tags, list):
            all_tags.extend(tags[:3])
    if len(all_tags) >= 3:
        counts = Counter(all_tags)
        top = counts.most_common(10)
        return [t[0] for t in top], [float(t[1]) for t in top]

    # Bridge attributes
    all_attrs: list[str] = []
    for r in results:
        attrs = r.get("bridge_attributes") or []
        if isinstance(attrs, list):
            all_attrs.extend(attrs)
    if len(all_attrs) >= 3:
        counts = Counter(all_attrs)
        return list(counts.keys()), [float(v) for v in counts.values()]

    # Final fallback: group by scope
    counts = Counter(r.get("scope", "unknown") for r in results)
    return list(counts.keys()), [float(v) for v in counts.values()]


# ---------------------------------------------------------------------------
# Degradation messaging
# ---------------------------------------------------------------------------


def _degradation_reason(requested: str, data_shape: str) -> str:
    if data_shape == "empty":
        return "No data matched your query."
    if requested in _TEMPORAL_TYPES and data_shape != "time_series":
        return f"A {requested.replace('_', ' ')} needs time-series data, but the results are {data_shape}."
    if requested in _MATRIX_TYPES and data_shape not in ("multivariate", "categorical"):
        return f"A {requested.replace('_', ' ')} needs multiple numeric dimensions."
    return f"The available data ({data_shape}) doesn't fit a {requested.replace('_', ' ')} chart."


def _degradation_suggestion(data_shape: str) -> str:
    best = _best_fallback_type(data_shape)
    shape_hints = {
        "time_series": "Your data has timestamps — try asking for a trend or timeline view.",
        "multivariate": "Your data has multiple numeric fields — try a scatter plot or comparison.",
        "categorical": "Your data groups naturally into categories — a bar chart or breakdown works well.",
        "minimal": "There's limited data — try broadening your query for more results.",
    }
    hint = shape_hints.get(data_shape, "")
    return f"Showing a {best} chart instead. {hint}"


# ---------------------------------------------------------------------------
# Chart data builder
# ---------------------------------------------------------------------------


def _build_chart_data(
    results: list[dict[str, Any]],
    chart_type: str,
    query: str,
) -> tuple[ChartData | None, AnalyticsDegradation | None]:
    """Build chart data from recall results, degrading gracefully when the
    requested chart type does not fit the data shape."""
    if not results:
        return None, AnalyticsDegradation(
            requested_type=chart_type, actual_type="none",
            reason="No data found matching your query.",
            data_available="none",
            suggestion="Try broadening your search or asking about a different topic.",
        )

    data_shape = _detect_data_shape(results)
    degradation = None

    if not _chart_fits_data(chart_type, data_shape):
        fallback = _best_fallback_type(data_shape)
        degradation = AnalyticsDegradation(
            requested_type=chart_type, actual_type=fallback,
            reason=_degradation_reason(chart_type, data_shape),
            data_available=f"{len(results)} records, shape: {data_shape}",
            suggestion=_degradation_suggestion(data_shape),
        )
        chart_type = fallback

    if chart_type not in _SUPPORTED_TYPES:
        if not degradation:
            degradation = AnalyticsDegradation(
                requested_type=chart_type, actual_type="bar",
                reason=f"Chart type '{chart_type}' is not yet supported. Showing bar chart.",
                data_available=f"{len(results)} records, shape: {data_shape}",
                suggestion="Supported types: bar, pie, line, donut, hbar, area.",
            )
        chart_type = "bar"

    if chart_type in ("line", "area") and data_shape == "time_series":
        labels, values = _extract_time_series(results)
    else:
        labels, values = _extract_categorical(results, query)

    if not labels:
        return None, AnalyticsDegradation(
            requested_type=chart_type, actual_type="none",
            reason="Could not extract chart data from the query results.",
            data_available=f"{len(results)} records, shape: {data_shape}",
            suggestion="Try asking a more specific question about a control family or persona.",
        )

    return ChartData(
        type=chart_type, title=query,
        labels=labels, values=values,
        colors=_CHART_COLORS[:len(labels)],
    ), degradation


# ---------------------------------------------------------------------------
# Describe helper
# ---------------------------------------------------------------------------


def _describe_results(results: list[dict[str, Any]], query: str, scope: str) -> str:
    """Generate a human-readable summary of recall results."""
    n = len(results)
    scopes = Counter(r.get("scope", scope) for r in results)
    scope_summary = ", ".join(f"{s}: {c}" for s, c in scopes.most_common(5))

    from ...lessons.store import get_mind_tags
    all_tags: list[str] = []
    for r in results:
        tags = r.get("tags") or get_mind_tags(r) or []
        if isinstance(tags, list):
            all_tags.extend(tags[:3])
    top_tags = [t for t, _ in Counter(all_tags).most_common(5)] if all_tags else []

    parts = [f"Found {n} records for '{query}'."]
    if scope_summary:
        parts.append(f"Scopes: {scope_summary}.")
    if top_tags:
        parts.append(f"Top tags: {', '.join(top_tags)}.")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Infer chart type from query text
# ---------------------------------------------------------------------------


def _infer_chart_type(query: str) -> str:
    """Guess chart type from keywords in the query."""
    q = query.lower()
    if any(kw in q for kw in ("pie", "breakdown", "proportion", "share")):
        return "pie"
    if any(kw in q for kw in ("line", "trend", "timeline", "over time")):
        return "line"
    if any(kw in q for kw in ("area", "cumulative")):
        return "area"
    if any(kw in q for kw in ("horizontal", "hbar")):
        return "hbar"
    if any(kw in q for kw in ("donut", "ring")):
        return "donut"
    return "bar"


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


@router.post("/run")
async def run_analytics(body: AnalyticsRequest) -> AnalyticsResponse:
    """Run the analytics pipeline: recall existing data, describe it, optionally
    build chart data.

    Does NOT duplicate recall AQL — delegates to MemoryClient.recall().
    """
    if not body.q.strip():
        raise HTTPException(status_code=400, detail="Query cannot be empty")

    # -- Step 1: Recall via the shared MemoryClient --------------------------
    try:
        loop = asyncio.get_running_loop()
        recall_result = await loop.run_in_executor(
            None,
            partial(
                _client.recall,
                q=body.q,
                scope=body.scope,
                k=body.limit,
            ),
        )
    except (ConnectionError, OSError) as exc:
        logger.error("Analytics recall failed (connectivity): {}", exc)
        raise HTTPException(status_code=503, detail=f"Database unavailable: {exc}") from exc
    except Exception as exc:
        logger.error("Analytics recall failed: {}", exc)
        raise HTTPException(status_code=500, detail=f"Recall error: {exc}") from exc

    results: list[dict[str, Any]] = recall_result.get("results", [])

    # -- Step 2: No data → explicit status -----------------------------------
    if not results:
        return AnalyticsResponse(
            status="no_data",
            description="No matching records found",
            record_count=0,
        )

    # -- Step 3: Describe ----------------------------------------------------
    description = _describe_results(results, body.q, body.scope)

    if body.type == "describe":
        return AnalyticsResponse(
            status="ok",
            description=description,
            record_count=len(results),
        )

    # -- Step 4: Chart / figure ----------------------------------------------
    chart_type = _infer_chart_type(body.q)
    chart_data, degradation = _build_chart_data(results, chart_type, body.q)

    final_chart_type = chart_type
    if degradation and degradation.actual_type != "none":
        final_chart_type = degradation.actual_type
        description = f"{description} (showing {final_chart_type} — {degradation.reason})"

    return AnalyticsResponse(
        status="ok",
        description=description,
        chart_type=final_chart_type,
        chart_data=chart_data,
        record_count=len(results),
        degraded=degradation,
    )

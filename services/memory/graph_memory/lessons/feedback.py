from __future__ import annotations
import time
import typer
from ..arango_client import get_db
from ..setup_schema import ensure_collections_and_view
from ..events import log_event
from loguru import logger

app = typer.Typer(add_completion=False)


def _submit_impl(
    lesson_title: str = typer.Option(...),
    lesson_scope: str = typer.Option("", help="Optional scope"),
    helpful: str = typer.Option("true", help="true|false"),
    note: str = typer.Option(""),
):
    ensure_collections_and_view()
    db = get_db()
    ts = int(time.time())
    # Find lesson id
    seed = list(db.aql.execute(
        "FOR d IN lessons_v2 FILTER d.title==@t AND (@s=='' OR d.scope==@s) LIMIT 1 RETURN d._id",
        bind_vars={'t': lesson_title, 's': lesson_scope or ''}
    ))
    if not seed:
        print('lesson not found')
        raise typer.Exit(2)
    lid = seed[0]
    # Update usage stats and note
    aql = """
    LET d = DOCUMENT(@lid)
    UPDATE d WITH { usage_count: (d.usage_count ? d.usage_count : 0) + 1, used_at: @ts } IN lessons_v2
    RETURN NEW._id
    """
    list(db.aql.execute(aql, bind_vars={'lid': lid, 'ts': ts}))
    # Best-effort mid-term marker: usage_count >= 3 and used within last 30 days
    try:
        db.aql.execute(
            """
            LET d = DOCUMENT(@lid)
            LET cnt = TO_NUMBER(d.usage_count ? d.usage_count : 0)
            LET recent = TO_NUMBER(d.used_at ? d.used_at : 0)
            LET now = @ts
            LET is_mid = cnt >= 3 && (now - recent) <= (30*86400)
            UPDATE d WITH { is_midterm: is_mid } IN lessons_v2
            """,
            bind_vars={'lid': lid, 'ts': ts}
        )
    except Exception as exc:
        logger.error("_submit_impl caught error: {exc}", exc=exc)
    # Light long-term marker: mark as long when it's a summary or has high usage and recent helpful feedback
    try:
        db.aql.execute(
            """
            LET d = DOCUMENT(@lid)
            LET cnt = TO_NUMBER(d.usage_count ? d.usage_count : 0)
            LET is_long = (d.is_summary == true) OR (cnt >= 5)
            UPDATE d WITH { is_longterm: is_long, last_promoted_at: @ts } IN lessons_v2
            """,
            bind_vars={'lid': lid, 'ts': ts}
        )
    except Exception as exc:
        logger.error("_submit_impl caught error: {exc}", exc=exc)
    # Adjust edges from/to this lesson lightly based on feedback
    adj = 0.02 if str(helpful).lower() in ("1","true","yes","y") else -0.02
    aql2 = """
    FOR e IN lesson_edges
      FILTER e._from==@lid OR e._to==@lid
      LET base = TO_NUMBER(e.confidence ? e.confidence : 0.5)
      LET nc = base + @adj
      LET clamped = nc > 1.0 ? 1.0 : (nc < 0.0 ? 0.0 : nc)
      UPDATE e WITH { confidence: clamped, last_verified_at: @ts } IN lesson_edges
    """
    db.aql.execute(aql2, bind_vars={'lid': lid, 'adj': adj, 'ts': ts})
    try:
        log_event(db, 'feedback', f"feedback for {lesson_title}", {'lesson': lid, 'helpful': str(helpful).lower(), 'note': note})
    except Exception as exc:
        logger.error("_submit_impl log_event failed: {exc}", exc=exc)
    print('feedback recorded')

@app.command("submit")
def submit_cli(
    lesson_title: str = typer.Option(...),
    lesson_scope: str = typer.Option("", help="Optional scope"),
    helpful: str = typer.Option("true", help="true|false"),
    note: str = typer.Option(""),
):
    return _submit_impl(lesson_title=lesson_title, lesson_scope=lesson_scope, helpful=helpful, note=note)


# Expose a callable function `submit(...)` for tests that import and call directly
# (keeps CLI unchanged)

# Export a callable alias for programmatic use (not used by smokes)

def submit_callable(lesson_title: str, lesson_scope: str = "", helpful: bool = True, note: str = ""):
    return _submit_impl(
        lesson_title=lesson_title,
        lesson_scope=lesson_scope,
        helpful="true" if bool(helpful) else "false",
        note=note,
    )

class _SubmitProxy:
    """Callable proxy that supports both function-style and Typer CLI invocations.

    - Function path: submit(lesson_title=..., lesson_scope=..., helpful=..., note=...)
      Calls _submit_impl directly without going through Click's parser.
    - CLI path: any positional args or no kwargs → delegate to underlying Typer app.
    """

    def __init__(self, app: typer.Typer):
        self._app = app

    def __call__(self, *args, **kwargs):  # type: ignore[override]
        keys = {"lesson_title", "lesson_scope", "helpful", "note"}
        if args == () and kwargs and set(kwargs.keys()).issubset(keys):
            return _submit_impl(
                lesson_title=kwargs.get("lesson_title", ""),
                lesson_scope=kwargs.get("lesson_scope", ""),
                helpful=str(kwargs.get("helpful", "true")).lower(),
                note=kwargs.get("note", ""),
            )
        # Fallback to Typer app behavior (CLI invocation)
        return self._app(*args, **kwargs)

    def __getattr__(self, name):
        # Delegate all other attributes (e.g., .command) to the Typer app
        return getattr(self._app, name)


# Export `submit` as a proxy over the module-level Typer app `app`
submit = _SubmitProxy(app)

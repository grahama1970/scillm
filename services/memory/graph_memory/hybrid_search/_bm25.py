"""BM25 search on ArangoSearch views."""
from typing import Any, Dict, List
from loguru import logger


def bm25_search_collection(
    db,
    view_name: str,
    query: str,
    scope: str = "",
    k: int = 10,
    search_fields: List[str] = None,
    return_fields: Dict[str, str] = None,
    extra_filters: str = "",
) -> List[Dict[str, Any]]:
    """BM25 search on any ArangoSearch view.

    Args:
        db: ArangoDB database connection
        view_name: Name of the ArangoSearch view
        query: Search query text
        scope: Optional scope filter
        k: Number of results
        search_fields: Fields to search in (e.g., ["name", "docstring"])
        return_fields: Fields to return as {aql_expr: alias} (e.g., {"doc.name": "name"})
        extra_filters: Additional AQL FILTER clauses

    Returns:
        List of results with BM25 scores
    """
    if not search_fields:
        search_fields = ["text"]

    if not return_fields:
        return_fields = {"doc._key": "_key"}

    # Build SEARCH clause
    search_conditions = " OR ".join([
        f"doc.{field} IN TOKENS(@query, 'text_en')"
        for field in search_fields
    ])

    # Build RETURN clause (alias: expression)
    return_items = ", ".join([
        f"{alias}: {expr}"
        for expr, alias in return_fields.items()
    ])

    aql = f"""
    FOR doc IN {view_name}
    SEARCH ANALYZER({search_conditions}, 'text_en')
    FILTER @scope == "" OR doc.scope == @scope
    {extra_filters}
    LET bm25 = BM25(doc)
    SORT bm25 DESC
    LIMIT @k
    RETURN {{
        {return_items},
        score: bm25
    }}
    """

    return list(db.aql.execute(aql, bind_vars={
        "query": query,
        "scope": scope,
        "k": k,
    }))


def search_code_symbols(
    db,
    query: str,
    scope: str = "",
    k: int = 10,
) -> List[Dict[str, Any]]:
    """Search code symbols using identity analyzer for underscore-delimited names.

    The text_en analyzer doesn't handle underscore-delimited code names well
    (stems test_create to test_creat). This function uses identity analyzer
    with pattern matching which works correctly for code symbol names.

    Args:
        db: ArangoDB database connection
        query: Search query (e.g., "test", "validate_input")
        scope: Optional scope filter
        k: Number of results

    Returns:
        List of code symbols with relevance scores
    """
    # Normalize query for pattern matching
    query_lower = query.lower().strip()

    # Use ArangoSearch with identity analyzer for exact/prefix/contains matching
    # Also search docstring with text_en for natural language queries
    aql = """
    FOR doc IN code_symbols_search
        SEARCH (
            ANALYZER(STARTS_WITH(doc.name, @query_lower), 'identity')
            OR ANALYZER(doc.name LIKE CONCAT('%', @query_lower, '%'), 'identity')
            OR ANALYZER(doc.docstring IN TOKENS(@query, 'text_en'), 'text_en')
        )
        FILTER @scope == "" OR doc.scope == @scope
        /* Boost scores below operate on ArangoSearch-filtered results only,
           not a full collection scan — CONTAINS(LOWER()) is acceptable here. */
        LET name_match = CONTAINS(LOWER(doc.name), @query_lower) ? 1 : 0
        LET prefix_match = STARTS_WITH(LOWER(doc.name), @query_lower) ? 0.5 : 0
        LET docstring_score = BM25(doc)
        LET score = name_match + prefix_match + (docstring_score / 10.0)
        SORT score DESC
        LIMIT @k
        RETURN {
            _key: doc._key,
            name: doc.name,
            kind: doc.kind,
            file: doc.file_path,
            line: doc.start_line,
            score: score
        }
    """

    return list(db.aql.execute(aql, bind_vars={
        "query": query,
        "query_lower": query_lower,
        "scope": scope,
        "k": k,
    }))

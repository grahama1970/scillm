"""Cross-database query utilities for ArangoDB.

Enables querying across project databases (memory, pi_mono, devops, etc.)
while maintaining project isolation.

Usage:
    from graph_memory.cross_db import CrossDBClient

    client = CrossDBClient()

    # Query single database
    results = client.query("lessons", "FOR doc IN lessons RETURN doc", limit=10)

    # Query multiple databases
    results = client.query_all(["lessons", "sparta"], "FOR doc IN lessons RETURN doc.title")

    # Search across all project databases
    results = client.recall_global("error handling", k=10)
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from arango import ArangoClient


# Known project databases
PROJECT_DATABASES = [
    "memory",       # memory project (primary)
    "lessons",      # legacy (deprecated, kept for migration)
    "sparta",       # sparta/devops
    "agents",       # agent system
    # Add more as needed
]


def get_arango_client() -> ArangoClient:
    """Get ArangoDB client."""
    host = os.getenv("ARANGO_HOST", "localhost")
    port = os.getenv("ARANGO_PORT", "8529")
    return ArangoClient(hosts=f"http://{host}:{port}")


class CrossDBClient:
    """Client for cross-database ArangoDB queries."""

    def __init__(
        self,
        username: Optional[str] = None,
        password: Optional[str] = None,
        databases: Optional[List[str]] = None,
    ):
        self.client = get_arango_client()
        self.username = username or os.getenv("ARANGO_USER", "root")
        self.password = password or os.getenv("ARANGO_PASS", "")
        self.databases = databases or PROJECT_DATABASES
        self._db_cache: Dict[str, Any] = {}

    def _get_db(self, db_name: str):
        """Get database connection (cached)."""
        if db_name not in self._db_cache:
            self._db_cache[db_name] = self.client.db(
                db_name,
                username=self.username,
                password=self.password,
            )
        return self._db_cache[db_name]

    def query(
        self,
        db_name: str,
        aql: str,
        bind_vars: Optional[Dict[str, Any]] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Execute AQL query on a single database."""
        db = self._get_db(db_name)
        cursor = db.aql.execute(aql, bind_vars=bind_vars or {})
        results = []
        for doc in cursor:
            if len(results) >= limit:
                break
            doc["_database"] = db_name
            results.append(doc)
        return results

    def query_all(
        self,
        databases: Optional[List[str]],
        aql: str,
        bind_vars: Optional[Dict[str, Any]] = None,
        limit_per_db: int = 50,
    ) -> List[Dict[str, Any]]:
        """Execute AQL query across multiple databases."""
        dbs = databases or self.databases
        all_results = []

        for db_name in dbs:
            try:
                results = self.query(db_name, aql, bind_vars, limit=limit_per_db)
                all_results.extend(results)
            except Exception as e:
                # Database might not exist or have the collection
                print(f"Warning: Query failed on {db_name}: {e}")

        return all_results

    def recall_global(
        self,
        query: str,
        k: int = 10,
        databases: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Recall across all project databases.

        Returns combined results with source database tagged.
        """
        dbs = databases or self.databases
        all_items = []
        errors = []

        aql = """
        FOR doc IN unified_search
            SEARCH ANALYZER(doc.problem IN TOKENS(@q, 'text_en'), 'text_en')
            OR ANALYZER(doc.solution IN TOKENS(@q, 'text_en'), 'text_en')
            OR ANALYZER(doc.title IN TOKENS(@q, 'text_en'), 'text_en')
            SORT BM25(doc) DESC
            LIMIT @k
            RETURN {
                _key: doc._key,
                title: doc.title,
                problem: doc.problem,
                solution: doc.solution,
                scope: doc.scope,
                tags: doc.tags,
                bm25: BM25(doc)
            }
        """

        for db_name in dbs:
            try:
                results = self.query(db_name, aql, {"q": query, "k": k}, limit=k)
                for r in results:
                    r["_source_db"] = db_name
                all_items.extend(results)
            except Exception as e:
                errors.append(f"{db_name}: {str(e)}")

        # Sort by BM25 score across all databases
        all_items.sort(key=lambda x: x.get("bm25", 0), reverse=True)

        return {
            "found": len(all_items) > 0,
            "items": all_items[:k],
            "meta": {
                "databases_queried": dbs,
                "total_found": len(all_items),
            },
            "errors": errors,
        }

    def list_databases(self) -> List[str]:
        """List all available project databases."""
        sys_db = self.client.db(
            "_system",
            username=self.username,
            password=self.password,
        )
        all_dbs = sys_db.databases()
        return [db for db in all_dbs if db in self.databases or not db.startswith("_")]

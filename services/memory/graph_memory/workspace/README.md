# workspace module

Workspace-aware ingestion pipeline: detect a workspace root (`workspace-detect`), ingest docs into lessons (`workspace-ingest`), and run detect→ingest→propose (`workspace-build`). Scoped to `workspace:<name>` by default. This is the paved path for turning existing docs into graph nodes for BM25/semantic/graph recall.

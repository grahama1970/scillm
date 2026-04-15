"""Tier 3: Proof queue and background worker.

ProofJob / ProofQueue — in-memory queue for background proof processing.
ArangoProofQueue — persistent ArangoDB-backed queue.
proof_worker — background worker that processes proof jobs.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from loguru import logger


# =============================================================================
# In-Memory Queue
# =============================================================================


@dataclass
class ProofJob:
    """A job in the proof queue."""
    lesson_key: str
    claim: str
    lean4_sketch: Optional[str] = None
    suggested_tactics: List[str] = field(default_factory=list)
    status: str = "pending"  # pending, proving, proved, failed
    attempts: int = 0
    max_attempts: int = 3
    created_at: datetime = field(default_factory=datetime.utcnow)
    last_attempt_at: Optional[datetime] = None
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


class ProofQueue:
    """In-memory proof queue for background processing.

    For production, replace with Redis/ArangoDB-backed queue.
    """

    def __init__(self):
        self._queue: List[ProofJob] = []
        self._processing: Dict[str, ProofJob] = {}
        self._completed: Dict[str, ProofJob] = {}
        self._lock = asyncio.Lock()

    async def push(
        self,
        lesson_key: str,
        claim: str,
        lean4_sketch: Optional[str] = None,
        suggested_tactics: Optional[List[str]] = None,
    ) -> ProofJob:
        """Add a job to the proof queue."""
        async with self._lock:
            job = ProofJob(
                lesson_key=lesson_key,
                claim=claim,
                lean4_sketch=lean4_sketch,
                suggested_tactics=suggested_tactics or [],
            )
            self._queue.append(job)
            logger.info("Queued proof job: {}", lesson_key)
            return job

    async def pop(self, timeout: float = 60.0) -> Optional[ProofJob]:
        """Get the next job from the queue."""
        start = time.time()
        while time.time() - start < timeout:
            async with self._lock:
                if self._queue:
                    job = self._queue.pop(0)
                    job.status = "proving"
                    job.last_attempt_at = datetime.utcnow()
                    self._processing[job.lesson_key] = job
                    return job
            await asyncio.sleep(0.5)
        return None

    async def complete(
        self,
        lesson_key: str,
        success: bool,
        result: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
    ) -> Optional[ProofJob]:
        """Mark a job as complete or failed."""
        async with self._lock:
            job = self._processing.pop(lesson_key, None)
            if not job:
                return None

            job.attempts += 1

            if success:
                job.status = "proved"
                job.result = result
                self._completed[lesson_key] = job
                logger.info("Proof succeeded: {}", lesson_key)
            elif job.attempts >= job.max_attempts:
                job.status = "failed"
                job.error = error
                self._completed[lesson_key] = job
                logger.warning("Proof failed after {} attempts: {}", job.attempts, lesson_key)
            else:
                # Retry with different tactics
                job.status = "pending"
                job.error = error
                self._queue.append(job)
                logger.info("Retrying proof ({}/{}): {}", job.attempts, job.max_attempts, lesson_key)

            return job

    async def status(self) -> Dict[str, Any]:
        """Get queue status."""
        async with self._lock:
            return {
                "pending": len(self._queue),
                "processing": len(self._processing),
                "completed": len(self._completed),
                "proved": sum(1 for j in self._completed.values() if j.status == "proved"),
                "failed": sum(1 for j in self._completed.values() if j.status == "failed"),
            }

    def get_job(self, lesson_key: str) -> Optional[ProofJob]:
        """Get a job by lesson key."""
        if lesson_key in self._completed:
            return self._completed[lesson_key]
        if lesson_key in self._processing:
            return self._processing[lesson_key]
        for job in self._queue:
            if job.lesson_key == lesson_key:
                return job
        return None


# =============================================================================
# ArangoDB-Backed Proof Queue (Persistent)
# =============================================================================


class ArangoProofQueue:
    """Persistent proof queue backed by ArangoDB.

    Schema for proof_jobs collection:
        _key: str (uuid)
        source_id: str (episodes/<key> or lessons/<key>)
        source_type: str ("episode" or "lesson")
        claim: str
        lean4_sketch: str | None
        suggested_tactics: list[str]
        status: str (pending, proving, proved, failed)
        attempts: int
        max_attempts: int
        created_at: int (unix ts)
        updated_at: int (unix ts)
        error: str | None
        proof_id: str | None (reference to proofs/<key> if proved)

    Schema for proofs collection:
        _key: str (uuid)
        source_id: str (episodes/<key> or lessons/<key>)
        claim: str
        lean4_code: str
        tactics_used: list[str]
        compile_time_ms: int
        created_at: int (unix ts)
    """

    def __init__(self):
        self._db = None

    def _get_db(self):
        if self._db is None:
            from graph_memory.arango_client import get_db
            from graph_memory.setup_schema import ensure_collections_and_view
            ensure_collections_and_view()
            self._db = get_db()
        return self._db

    async def push(
        self,
        source_id: str,
        claim: str,
        lean4_sketch: Optional[str] = None,
        suggested_tactics: Optional[List[str]] = None,
        source_type: str = "episode",
    ) -> Dict[str, Any]:
        """Add a job to the proof queue (persisted to ArangoDB)."""
        db = self._get_db()
        col = db.collection("proof_jobs")

        ts = int(time.time())
        job = {
            "_key": uuid.uuid4().hex[:16],
            "source_id": source_id,
            "source_type": source_type,
            "claim": claim,
            "lean4_sketch": lean4_sketch,
            "suggested_tactics": suggested_tactics or [],
            "status": "pending",
            "attempts": 0,
            "max_attempts": 3,
            "created_at": ts,
            "updated_at": ts,
            "error": None,
            "proof_id": None,
        }
        col.insert(job)
        logger.info("Queued proof job: {} -> proof_jobs/{}", source_id, job['_key'])
        return job

    async def pop(self, timeout: float = 60.0) -> Optional[Dict[str, Any]]:
        """Get the next pending job from the queue."""
        db = self._get_db()
        start = time.time()

        while time.time() - start < timeout:
            # Find and claim next pending job atomically
            cursor = db.aql.execute(
                """
                FOR job IN proof_jobs
                    FILTER job.status == "pending"
                    SORT job.created_at ASC
                    LIMIT 1
                    UPDATE job WITH {
                        status: "proving",
                        updated_at: @now
                    } IN proof_jobs
                    RETURN NEW
                """,
                bind_vars={"now": int(time.time())}
            )
            jobs = list(cursor)
            if jobs:
                return jobs[0]
            await asyncio.sleep(0.5)
        return None

    async def complete(
        self,
        job_key: str,
        success: bool,
        lean4_code: Optional[str] = None,
        tactics_used: Optional[List[str]] = None,
        compile_time_ms: Optional[int] = None,
        error: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Mark a job as complete or failed."""
        db = self._get_db()
        jobs_col = db.collection("proof_jobs")
        proofs_col = db.collection("proofs")

        # Get current job
        try:
            job = jobs_col.get(job_key)
        except Exception as exc:
            logger.error("Suppressed error in proof_assessment: {}", exc)
            return None

        if not job:
            return None

        ts = int(time.time())
        job["attempts"] = job.get("attempts", 0) + 1
        job["updated_at"] = ts

        if success and lean4_code:
            # Store proof in proofs collection
            proof = {
                "_key": uuid.uuid4().hex[:16],
                "source_id": job["source_id"],
                "claim": job["claim"],
                "lean4_code": lean4_code,
                "tactics_used": tactics_used or [],
                "compile_time_ms": compile_time_ms or 0,
                "created_at": ts,
            }
            proofs_col.insert(proof)

            job["status"] = "proved"
            job["proof_id"] = f"proofs/{proof['_key']}"
            job["error"] = None

            # Update source document with proof reference
            self._update_source_proof_status(
                job["source_id"],
                job["source_type"],
                "proved",
                job["proof_id"]
            )

            logger.info("Proof succeeded: {} -> {}", job['source_id'], job['proof_id'])

        elif job["attempts"] >= job.get("max_attempts", 3):
            job["status"] = "failed"
            job["error"] = error

            # Update source document
            self._update_source_proof_status(
                job["source_id"],
                job["source_type"],
                "failed",
                None
            )

            logger.warning("Proof failed after {} attempts: {}", job['attempts'], job['source_id'])
        else:
            # Retry
            job["status"] = "pending"
            job["error"] = error
            logger.info("Retrying proof ({}/{}): {}", job['attempts'], job.get('max_attempts', 3), job['source_id'])

        jobs_col.update(job)
        return job

    def _update_source_proof_status(
        self,
        source_id: str,
        source_type: str,
        status: str,
        proof_id: Optional[str],
    ):
        """Update the source document (episode/lesson) with proof status."""
        db = self._get_db()
        collection_name = "episodes" if source_type == "episode" else "lessons"

        try:
            # Extract key from source_id (e.g., "episodes/abc123" -> "abc123")
            key = source_id.split("/")[-1] if "/" in source_id else source_id
            col = db.collection(collection_name)
            col.update({
                "_key": key,
                "proof_status": status,
                "proof_id": proof_id,
            })
        except Exception as exc:
            logger.warning("Failed to update source proof status: {}", exc)

    async def status(self, scope: Optional[str] = None) -> Dict[str, Any]:
        """Get queue status."""
        db = self._get_db()

        # Count by status
        query = """
            LET pending = LENGTH(FOR j IN proof_jobs FILTER j.status == "pending" RETURN 1)
            LET proving = LENGTH(FOR j IN proof_jobs FILTER j.status == "proving" RETURN 1)
            LET proved = LENGTH(FOR j IN proof_jobs FILTER j.status == "proved" RETURN 1)
            LET failed = LENGTH(FOR j IN proof_jobs FILTER j.status == "failed" RETURN 1)
            LET total_proofs = LENGTH(FOR p IN proofs RETURN 1)
            RETURN {
                pending: pending,
                proving: proving,
                proved: proved,
                failed: failed,
                total_proofs: total_proofs
            }
        """
        cursor = db.aql.execute(query)
        result = list(cursor)
        return result[0] if result else {
            "pending": 0,
            "proving": 0,
            "proved": 0,
            "failed": 0,
            "total_proofs": 0,
        }

    def get_job(self, source_id: str) -> Optional[Dict[str, Any]]:
        """Get the latest job for a source_id."""
        db = self._get_db()
        cursor = db.aql.execute(
            """
            FOR job IN proof_jobs
                FILTER job.source_id == @source_id
                SORT job.created_at DESC
                LIMIT 1
                RETURN job
            """,
            bind_vars={"source_id": source_id}
        )
        jobs = list(cursor)
        return jobs[0] if jobs else None

    def get_proof(self, proof_id: str) -> Optional[Dict[str, Any]]:
        """Get a proof by ID."""
        db = self._get_db()
        key = proof_id.split("/")[-1] if "/" in proof_id else proof_id
        try:
            return db.collection("proofs").get(key)
        except Exception as exc:
            logger.error("Suppressed error in proof_assessment: {}", exc)
            return None


# =============================================================================
# Background Worker
# =============================================================================

async def proof_worker(
    queue: ProofQueue,
    concurrency: int = 4,
    stop_event: Optional[asyncio.Event] = None,
):
    """Background worker that processes proof jobs.

    Args:
        queue: The proof queue to process
        concurrency: Number of parallel proof attempts
        stop_event: Event to signal worker shutdown
    """
    logger.info("Proof worker started with concurrency={}", concurrency)

    async def process_job(job: ProofJob):
        """Process a single proof job."""
        try:
            from scillm.integrations.certainly import prove_requirement
        except ImportError:
            await queue.complete(
                job.lesson_key,
                success=False,
                error="certainly-prover not available",
            )
            return

        try:
            # Use sketch and tactics from assessment
            tactics = job.suggested_tactics or ["simp", "omega", "decide"]

            result = await prove_requirement(
                requirement=job.claim,
                tactics=tactics,
            )

            if result.get("ok"):
                await queue.complete(
                    job.lesson_key,
                    success=True,
                    result={
                        "lean4_code": result.get("lean4_code"),
                        "tactic_used": result.get("tactic_used"),
                        "compile_time_ms": result.get("compile_time_ms"),
                    },
                )
            else:
                await queue.complete(
                    job.lesson_key,
                    success=False,
                    error=result.get("failure_reason", "Unknown error"),
                )

        except Exception as exc:
            logger.error("Proof job failed for {}: {}", job.lesson_key, exc)
            await queue.complete(
                job.lesson_key,
                success=False,
                error=str(exc),
            )

    semaphore = asyncio.Semaphore(concurrency)

    async def worker():
        while stop_event is None or not stop_event.is_set():
            job = await queue.pop(timeout=5.0)
            if job:
                async with semaphore:
                    await process_job(job)

    # Run workers
    workers = [asyncio.create_task(worker()) for _ in range(concurrency)]

    try:
        if stop_event:
            await stop_event.wait()
        else:
            await asyncio.gather(*workers)
    finally:
        for w in workers:
            w.cancel()
        logger.info("Proof worker stopped")

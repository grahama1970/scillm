"""Teacher-Student pattern for continuous model improvement.

Common design pattern for all monitor-* skills and assessment pipelines:

    Phase 1 (Bootstrap): Teacher (SciLLM) judges 100% of a stratified sample
    Phase 2 (Train):     /create-gpt trains small GPT on teacher labels
    Phase 3 (Deploy):    Small GPT handles 100%, teacher validates random N%
    Phase 4 (Correct):   Disagreements become new training data
    Phase 5 (Anneal):    Sample rate decreases as agreement improves

Brandon QRA assessment is the canonical example:
    - Teacher: SciLLM (accurate profile) grades QRA reasoning traces
    - Student: Small GPT (Qwen2.5-1.5B fine-tuned) learns Brandon's rubric
    - Validation: Brandon spot-checks 5% stratified sample each cycle
    - Retraining: When disagreement > 10%, trigger /create-gpt iterate

Usage:
    from graph_memory.llm.teacher_student import TeacherStudentLoop
from loguru import logger

    loop = TeacherStudentLoop(
        task_id="qra_assess",
        teacher_prompt=QRA_TEACHER_PROMPT,
        student_model_path="models/qra-assessor/model.gguf",
        state_dir="data/teacher_student/qra_assess",
        stratify_key="framework",  # Stratify by CWE/D3FEND/SPARTA/etc.
    )

    # Phase 1: Bootstrap — teacher labels a sample
    labels = await loop.bootstrap(items, sample_size=500)

    # Phase 2: Train — export JSONL and trigger /create-gpt
    loop.export_training_data("training.jsonl")

    # Phase 3: Deploy — student handles items, teacher validates sample
    results = loop.run_with_validation(items, sample_rate=0.05)

    # Phase 4: Check if retraining needed
    if loop.needs_retraining():
        loop.export_training_data("retrain.jsonl", include_corrections=True)
"""
from __future__ import annotations

import json
import os
import random
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .client import _log
from .router import (
    InferenceRouter,
    RouteResult,
    TierConfig,
    Tier,
    _extract_json,
)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class AnnealSchedule:
    """Sample rate annealing schedule based on agreement rate.

    As the student model improves (higher agreement with teacher),
    we reduce the teacher validation sample rate to save cost.
    """
    # (min_agreement, sample_rate) — sorted by agreement ascending
    stages: List[Tuple[float, float]] = field(default_factory=lambda: [
        (0.0,  0.10),   # <80% agreement: validate 10% of items
        (0.80, 0.05),   # 80-90% agreement: validate 5%
        (0.90, 0.03),   # 90-95% agreement: validate 3%
        (0.95, 0.02),   # 95-98% agreement: validate 2%
        (0.98, 0.01),   # 98%+ agreement: validate 1% (never 0!)
    ])

    def get_sample_rate(self, agreement_rate: float) -> float:
        """Get the sample rate for the current agreement level."""
        rate = self.stages[0][1]  # default to highest rate
        for min_agreement, sample_rate in self.stages:
            if agreement_rate >= min_agreement:
                rate = sample_rate
        return rate


@dataclass
class TeacherStudentState:
    """Persistent state for the teacher-student loop."""
    task_id: str
    phase: str = "bootstrap"  # bootstrap, training, deployed, retraining
    total_teacher_labels: int = 0
    total_student_predictions: int = 0
    total_validations: int = 0
    agreements: int = 0
    disagreements: int = 0
    corrections: List[Dict[str, Any]] = field(default_factory=list)
    retrain_count: int = 0
    current_sample_rate: float = 0.10
    agreement_history: List[Dict[str, float]] = field(default_factory=list)
    last_updated: float = 0.0

    @property
    def agreement_rate(self) -> float:
        total = self.agreements + self.disagreements
        if total == 0:
            return 0.0
        return self.agreements / total

    @property
    def needs_retraining(self) -> bool:
        """Retrain when recent disagreement rate exceeds 10%."""
        if self.total_validations < 50:
            return False  # Not enough data
        return self.agreement_rate < 0.90

    def record_validation(
        self,
        teacher_result: Dict[str, Any],
        student_result: Dict[str, Any],
        agreed: bool,
        input_data: Dict[str, Any],
    ) -> None:
        self.total_validations += 1
        if agreed:
            self.agreements += 1
        else:
            self.disagreements += 1
            self.corrections.append({
                "input": input_data,
                "teacher": teacher_result,
                "student": student_result,
                "timestamp": time.time(),
            })
        self.last_updated = time.time()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "phase": self.phase,
            "total_teacher_labels": self.total_teacher_labels,
            "total_student_predictions": self.total_student_predictions,
            "total_validations": self.total_validations,
            "agreements": self.agreements,
            "disagreements": self.disagreements,
            "agreement_rate": round(self.agreement_rate, 4),
            "corrections_count": len(self.corrections),
            "retrain_count": self.retrain_count,
            "current_sample_rate": self.current_sample_rate,
            "agreement_history": self.agreement_history[-20:],
            "needs_retraining": self.needs_retraining,
            "last_updated": self.last_updated,
        }

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2))

    @classmethod
    def load(cls, path: Path, task_id: str) -> TeacherStudentState:
        if path.exists():
            data = json.loads(path.read_text())
            state = cls(task_id=task_id)
            for k, v in data.items():
                if hasattr(state, k) and k != "agreement_rate" and k != "needs_retraining":
                    setattr(state, k, v)
            return state
        return cls(task_id=task_id)


# ---------------------------------------------------------------------------
# Stratified sampling
# ---------------------------------------------------------------------------

def stratified_sample(
    items: List[Dict[str, Any]],
    sample_rate: float,
    stratify_key: Optional[str] = None,
    min_per_stratum: int = 1,
) -> Tuple[List[int], List[int]]:
    """Split items into sample (for teacher validation) and remainder.

    Args:
        items: List of items to sample from
        sample_rate: Fraction to sample (0.0-1.0)
        stratify_key: Key in each item to stratify by (e.g., "framework")
        min_per_stratum: Minimum items per stratum in sample

    Returns:
        (sample_indices, remainder_indices)
    """
    if not items:
        return [], []

    n = len(items)
    target_sample = max(1, int(n * sample_rate))

    if not stratify_key:
        # Simple random sample
        indices = list(range(n))
        random.shuffle(indices)
        return indices[:target_sample], indices[target_sample:]

    # Group by stratum
    strata: Dict[str, List[int]] = defaultdict(list)
    for i, item in enumerate(items):
        key = str(item.get(stratify_key, "unknown"))
        strata[key].append(i)

    sample_indices = []
    remainder_indices = []

    for stratum_key, indices in strata.items():
        stratum_sample = max(min_per_stratum, int(len(indices) * sample_rate))
        stratum_sample = min(stratum_sample, len(indices))

        random.shuffle(indices)
        sample_indices.extend(indices[:stratum_sample])
        remainder_indices.extend(indices[stratum_sample:])

    return sample_indices, remainder_indices


# ---------------------------------------------------------------------------
# Agreement checking
# ---------------------------------------------------------------------------

def check_agreement(
    teacher: Dict[str, Any],
    student: Dict[str, Any],
    agreement_fields: Optional[List[str]] = None,
    tolerance: float = 0.1,
) -> Tuple[bool, List[str]]:
    """Check if teacher and student agree on key fields.

    Args:
        teacher: Teacher model output
        student: Student model output
        agreement_fields: Fields to check (default: grade, keep, stance)
        tolerance: Numeric tolerance for float comparisons

    Returns:
        (agreed: bool, disagreement_reasons: list)
    """
    if agreement_fields is None:
        agreement_fields = ["grade", "keep", "stance", "valid", "healthy"]

    reasons = []

    for field_name in agreement_fields:
        t_val = teacher.get(field_name)
        s_val = student.get(field_name)

        if t_val is None and s_val is None:
            continue

        if t_val is None or s_val is None:
            reasons.append(f"{field_name}: teacher={t_val}, student={s_val}")
            continue

        # Boolean comparison
        if isinstance(t_val, bool) or isinstance(s_val, bool):
            if bool(t_val) != bool(s_val):
                reasons.append(f"{field_name}: teacher={t_val}, student={s_val}")
            continue

        # Numeric comparison with tolerance
        try:
            t_num = float(t_val)
            s_num = float(s_val)
            if abs(t_num - s_num) > tolerance:
                reasons.append(
                    f"{field_name}: teacher={t_num:.3f}, student={s_num:.3f} "
                    f"(delta={abs(t_num-s_num):.3f})"
                )
            continue
        except (ValueError, TypeError):
            pass

        # String comparison (case-insensitive)
        if str(t_val).lower() != str(s_val).lower():
            reasons.append(f"{field_name}: teacher={t_val}, student={s_val}")

    return len(reasons) == 0, reasons


# ---------------------------------------------------------------------------
# Teacher-Student Loop
# ---------------------------------------------------------------------------

class TeacherStudentLoop:
    """Reusable teacher-student loop for any monitor/assessment skill.

    This is the common design pattern:
    1. Teacher (large model) labels a stratified sample
    2. Student (small GPT) learns from teacher labels via /create-gpt
    3. Student handles 100% of items, teacher validates N%
    4. Disagreements feed back into training data
    5. Sample rate anneals as agreement improves

    Canonical examples:
    - Brandon QRA assessment (teacher=SciLLM accurate, student=qra-assessor GPT)
    - Edge scoring (teacher=SciLLM, student=edge-scorer GPT)
    - Monitor health checks (teacher=SciLLM, student=health-checker GPT)
    - Edge verification (teacher=Chutes sonar, student=verify GPT)
    """

    def __init__(
        self,
        task_id: str,
        teacher_prompt: str,
        state_dir: str,
        student_model_path: Optional[str] = None,
        student_endpoint: Optional[str] = None,
        teacher_model: Optional[str] = None,
        teacher_profile: str = "accurate",
        stratify_key: Optional[str] = None,
        agreement_fields: Optional[List[str]] = None,
        agreement_tolerance: float = 0.1,
        anneal_schedule: Optional[AnnealSchedule] = None,
        prompt_builder: Optional[Callable[[Dict[str, Any]], str]] = None,
    ):
        self.task_id = task_id
        self.teacher_prompt = teacher_prompt
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)

        self.stratify_key = stratify_key
        self.agreement_fields = agreement_fields
        self.agreement_tolerance = agreement_tolerance
        self.anneal = anneal_schedule or AnnealSchedule()

        # Build prompt from template + input data
        self._prompt_builder = prompt_builder or self._default_prompt_builder

        # Load persistent state
        self.state = TeacherStudentState.load(
            self.state_dir / "state.json", task_id
        )

        # Build routers
        self._teacher_config = TierConfig.scillm(
            model=teacher_model,
            profile=teacher_profile,
            name="teacher",
            max_tokens=1024,
        )

        student_tiers = []
        if student_model_path:
            student_tiers.append(TierConfig.small_gpt(
                model_path=student_model_path,
                min_confidence=0.0,  # Always accept student output
                name="student",
            ))
        if student_endpoint:
            student_tiers.append(TierConfig.small_gpt(
                model_endpoint=student_endpoint,
                min_confidence=0.0,
                name="student",
            ))

        self._has_student = len(student_tiers) > 0
        if self._has_student:
            self._student_router = InferenceRouter(
                f"{task_id}_student",
                tiers=student_tiers,
                prompt_builder=self._prompt_builder,
            )

    def _default_prompt_builder(self, input_data: Dict[str, Any]) -> str:
        """Build prompt from teacher template + input data."""
        input_json = json.dumps(input_data, ensure_ascii=False, indent=2)
        return f"{self.teacher_prompt}\n\nInput:\n{input_json}"

    # ------------------------------------------------------------------
    # Phase 1: Bootstrap — teacher labels everything
    # ------------------------------------------------------------------

    def bootstrap(
        self,
        items: List[Dict[str, Any]],
        sample_size: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Phase 1: Teacher labels a sample to create initial training data.

        Args:
            items: Items to label
            sample_size: Number of items to label (default: all)

        Returns:
            List of {input, label, teacher_model, latency_ms}
        """
        from .client import call_llm_json

        if sample_size and sample_size < len(items):
            sample_idx, _ = stratified_sample(
                items, sample_size / len(items), self.stratify_key
            )
            sample = [items[i] for i in sample_idx]
        else:
            sample = items

        labels = []
        for item in sample:
            prompt = self._prompt_builder(item)
            t0 = time.time()
            try:
                payload, model = call_llm_json(
                    prompt,
                    profile=self._teacher_config.profile,
                    model=self._teacher_config.model,
                    max_tokens=self._teacher_config.max_tokens,
                )
                latency_ms = int((time.time() - t0) * 1000)
                labels.append({
                    "input": item,
                    "label": payload,
                    "teacher_model": model,
                    "latency_ms": latency_ms,
                })
                self.state.total_teacher_labels += 1
            except Exception as e:
                _log("teacher_student.bootstrap_error", error=str(e))

        self.state.phase = "bootstrap_complete"
        self._save_state()
        self._save_labels(labels, "bootstrap_labels.jsonl")

        _log(
            "teacher_student.bootstrap_complete",
            task=self.task_id,
            labels=len(labels),
            items=len(items),
        )
        return labels

    # ------------------------------------------------------------------
    # Phase 2: Export training data for /create-gpt
    # ------------------------------------------------------------------

    def export_training_data(
        self,
        output_path: str,
        include_corrections: bool = True,
    ) -> int:
        """Phase 2: Export labeled data as JSONL for /create-gpt training.

        Format: {"input": {...}, "output": {...}} per line
        Compatible with /create-gpt prepare --input

        Args:
            output_path: Path to write JSONL
            include_corrections: Include teacher corrections from Phase 4

        Returns:
            Number of examples exported
        """
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)

        count = 0
        with open(output, "w") as f:
            # Bootstrap labels
            bootstrap_path = self.state_dir / "bootstrap_labels.jsonl"
            if bootstrap_path.exists():
                for line in bootstrap_path.read_text().splitlines():
                    if not line.strip():
                        continue
                    try:
                        entry = json.loads(line)
                        training_example = {
                            "input": entry["input"],
                            "output": entry["label"],
                        }
                        f.write(json.dumps(training_example) + "\n")
                        count += 1
                    except Exception as exc:
                        logger.error("Suppressed error in teacher_student: {}", exc)
                        continue

            # Corrections (teacher overrides of student mistakes)
            if include_corrections and self.state.corrections:
                for correction in self.state.corrections:
                    training_example = {
                        "input": correction["input"],
                        "output": correction["teacher"],  # Teacher is ground truth
                    }
                    f.write(json.dumps(training_example) + "\n")
                    count += 1

        _log(
            "teacher_student.export",
            task=self.task_id,
            output=str(output),
            examples=count,
            corrections=len(self.state.corrections) if include_corrections else 0,
        )
        return count

    # ------------------------------------------------------------------
    # Phase 3: Deploy with validation
    # ------------------------------------------------------------------

    def run_with_validation(
        self,
        items: List[Dict[str, Any]],
        sample_rate: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """Phase 3: Student handles all items, teacher validates a sample.

        Args:
            items: Items to process
            sample_rate: Override sample rate (default: auto from anneal schedule)

        Returns:
            List of results with tier_used and validation metadata
        """
        if not self._has_student:
            raise RuntimeError(
                "No student model configured. Run bootstrap first, "
                "then train with /create-gpt, then set student_model_path."
            )

        # Determine sample rate
        if sample_rate is None:
            sample_rate = self.anneal.get_sample_rate(self.state.agreement_rate)
        self.state.current_sample_rate = sample_rate

        # Split into validation sample and remainder
        sample_indices, remainder_indices = stratified_sample(
            items, sample_rate, self.stratify_key
        )

        results = [None] * len(items)

        # Process remainder with student only (no teacher validation)
        for idx in remainder_indices:
            student_result = self._student_router.route(items[idx])
            results[idx] = {
                "result": student_result.payload,
                "tier": "student",
                "validated": False,
                "confidence": student_result.confidence,
            }
            self.state.total_student_predictions += 1

        # Process sample with both student AND teacher
        from .client import call_llm_json

        for idx in sample_indices:
            # Student prediction
            student_result = self._student_router.route(items[idx])
            self.state.total_student_predictions += 1

            # Teacher validation
            prompt = self._prompt_builder(items[idx])
            try:
                teacher_payload, teacher_model = call_llm_json(
                    prompt,
                    profile=self._teacher_config.profile,
                    model=self._teacher_config.model,
                    max_tokens=self._teacher_config.max_tokens,
                )
                self.state.total_teacher_labels += 1

                # Check agreement
                agreed, reasons = check_agreement(
                    teacher_payload,
                    student_result.payload,
                    self.agreement_fields,
                    self.agreement_tolerance,
                )

                self.state.record_validation(
                    teacher_payload,
                    student_result.payload,
                    agreed,
                    items[idx],
                )

                # Use teacher result when they disagree (teacher is authority)
                final_result = student_result.payload if agreed else teacher_payload

                results[idx] = {
                    "result": final_result,
                    "tier": "student" if agreed else "teacher_override",
                    "validated": True,
                    "agreed": agreed,
                    "disagreement_reasons": reasons if not agreed else [],
                    "student_result": student_result.payload,
                    "teacher_result": teacher_payload,
                    "confidence": student_result.confidence,
                }

                if not agreed:
                    _log(
                        "teacher_student.disagreement",
                        task=self.task_id,
                        reasons=reasons,
                    )

            except Exception as e:
                # Teacher failed — use student result
                results[idx] = {
                    "result": student_result.payload,
                    "tier": "student",
                    "validated": False,
                    "teacher_error": str(e),
                    "confidence": student_result.confidence,
                }

        # Update agreement history
        self.state.agreement_history.append({
            "timestamp": time.time(),
            "agreement_rate": self.state.agreement_rate,
            "sample_rate": sample_rate,
            "validations": len(sample_indices),
        })

        self.state.phase = "deployed"
        self._save_state()

        _log(
            "teacher_student.validation_cycle",
            task=self.task_id,
            total_items=len(items),
            validated=len(sample_indices),
            agreement_rate=self.state.agreement_rate,
            sample_rate=sample_rate,
            needs_retraining=self.state.needs_retraining,
        )

        return [r for r in results if r is not None]

    # ------------------------------------------------------------------
    # Phase 4: Retraining check
    # ------------------------------------------------------------------

    def needs_retraining(self) -> bool:
        """Check if the student model needs retraining."""
        return self.state.needs_retraining

    def get_correction_count(self) -> int:
        """Number of teacher corrections accumulated."""
        return len(self.state.corrections)

    def trigger_retrain(self, output_path: str) -> int:
        """Export training data and mark state for retraining.

        Returns number of examples exported (bootstrap + corrections).
        """
        count = self.export_training_data(output_path, include_corrections=True)
        self.state.phase = "retraining"
        self.state.retrain_count += 1

        # Clear corrections after export (they're now in the training set)
        self.state.corrections = []
        self.state.agreements = 0
        self.state.disagreements = 0
        self._save_state()

        _log(
            "teacher_student.retrain_triggered",
            task=self.task_id,
            retrain_count=self.state.retrain_count,
            examples=count,
        )
        return count

    # ------------------------------------------------------------------
    # Status & persistence
    # ------------------------------------------------------------------

    def status(self) -> Dict[str, Any]:
        """Get current loop status."""
        return {
            **self.state.to_dict(),
            "anneal_schedule": [
                {"min_agreement": a, "sample_rate": r}
                for a, r in self.anneal.stages
            ],
        }

    def _save_state(self) -> None:
        self.state.save(self.state_dir / "state.json")

    def _save_labels(self, labels: List[Dict], filename: str) -> None:
        path = self.state_dir / filename
        with open(path, "a") as f:
            for label in labels:
                f.write(json.dumps(label, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Pre-built teacher prompts for common use cases
# ---------------------------------------------------------------------------

QRA_TEACHER_PROMPT = """You are Brandon Bailey, SPARTA framework creator and QRA quality assessor.

You assess QRAs (Question-Reasoning-Answer) for the SPARTA space cybersecurity framework.
Your assessment covers BOTH structural quality AND semantic depth.

## Structural Checks
1. **Anchoring**: Does the answer reference the specific control/technique by ID and name?
2. **Grounding**: Is the answer grounded in cited sources, not hallucinated?
3. **Space terminology**: Does it use >= 2 framework-specific terms (CWE IDs, ATT&CK techniques, D3FEND artifacts, NIST controls, spacecraft terms)?
4. **Citations**: Are citations present and relevant to the claims made?
5. **Length**: Is the answer substantive (>150 chars with real content)?

## Semantic Checks (CRITICAL — these determine real quality)
6. **Reasoning quality**: Is the reasoning chain logically sound? Does the answer follow from the question through a clear chain of inference? Or is it meta-commentary, circular restatement, or vague platitudes?
7. **Taxonomy correctness**: Do the conceptual_tags and tactical_tags ACTUALLY match the answer content? A tag like "encryption" should only appear if the answer genuinely discusses encryption. Flag mismatched or irrelevant tags.
8. **Relevance / usefulness**: Is this a MEANINGFUL question that a space security practitioner would actually ask? Or is it trivial, obvious, or so generic it could apply to any domain? A good QRA teaches something specific.

## Augmentation (Generate Training Variants)
9. **Similar questions**: Generate 2-3 alternative phrasings of the question that would lead to the same answer. These must be genuine rephrasings, not trivial word swaps.
10. **Paraphrased answer**: Rewrite the answer in different words while preserving all technical accuracy and citations. This tests whether the content can survive rephrasing — hallucinated content often can't.

## Framework-Specific Grounding Thresholds
- D3FEND: fail < 0.60, warn < 0.70
- CWE: fail < 0.55, warn < 0.65
- ATT&CK: fail < 0.60, warn < 0.70
- NIST: fail < 0.55, warn < 0.65
- SPARTA: fail < 0.55, warn < 0.65

Return JSON:
{
    "grade": "PASS" | "WARN" | "FAIL",
    "confidence": 0.0-1.0,
    "grounding_check": 0.0-1.0,
    "anchoring_ok": true/false,
    "space_terms_ok": true/false,
    "reasoning_sound": true/false,
    "taxonomy_correct": true/false,
    "relevance_score": 0.0-1.0,
    "issues": ["list of specific issues found"],
    "rationale": "brief explanation of the grade",
    "similar_questions": ["alt question 1", "alt question 2"],
    "paraphrased_answer": "rewritten answer preserving technical accuracy"
}"""

EDGE_SCORE_TEACHER_PROMPT = """You are a Knowledge Graph relationship scorer.

Given two lessons (source and target), assess their relationship:
1. Should this edge be kept? (Is there a meaningful connection?)
2. What is the relationship weight? (0.0 = unrelated, 1.0 = essential)
3. What type of relationship is this?

Return JSON:
{
    "keep": true/false,
    "weight": 0.0-1.0,
    "confidence": 0.0-1.0,
    "type": "solves" | "depends_on" | "related_to" | "contradicts" | "verifies",
    "rationale": "brief explanation"
}"""

MONITOR_HEALTH_TEACHER_PROMPT = """You are a system health assessor.

Given health metrics and recent events, assess the system's health:
1. Is the system healthy?
2. What issues exist?
3. How severe are they?

Return JSON:
{
    "healthy": true/false,
    "confidence": 0.0-1.0,
    "issues": ["list of issues"],
    "severity": "low" | "medium" | "high" | "critical",
    "recommendation": "brief action recommendation"
}"""

EDGE_VERIFY_TEACHER_PROMPT = """You are a Knowledge Graph Auditor.

Given a source text (episode/turn) and a candidate target lesson,
classify their relationship:

Return JSON:
{
    "weight": 0.0-1.0,
    "stance": "verifies" | "contradicts" | "neutral",
    "confidence": 0.0-1.0,
    "rationale": "brief explanation"
}"""

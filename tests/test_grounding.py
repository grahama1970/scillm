"""Unit tests for source grounding verification in batch engine."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from scillm.batch import _resolve_source, _check_grounding


# ---------------------------------------------------------------------------
# _resolve_source
# ---------------------------------------------------------------------------


class TestResolveSource:
    def test_none_returns_none(self):
        assert _resolve_source(None) is None

    def test_empty_string_returns_none(self):
        assert _resolve_source("") is None
        assert _resolve_source("   ") is None

    def test_inline_text(self):
        assert _resolve_source("hello world") == "hello world"

    def test_inline_list(self):
        result = _resolve_source(["part one", "part two"])
        assert "part one" in result
        assert "part two" in result

    def test_file_path_reads_content(self, tmp_path):
        f = tmp_path / "source.txt"
        f.write_text("The AC-17 control requires multi-factor authentication.", encoding="utf-8")
        result = _resolve_source(str(f))
        assert "AC-17" in result
        assert "multi-factor" in result

    def test_file_path_list(self, tmp_path):
        f1 = tmp_path / "a.txt"
        f2 = tmp_path / "b.txt"
        f1.write_text("Alpha content", encoding="utf-8")
        f2.write_text("Beta content", encoding="utf-8")
        result = _resolve_source([str(f1), str(f2)])
        assert "Alpha" in result
        assert "Beta" in result

    def test_mixed_file_and_inline(self, tmp_path):
        f = tmp_path / "doc.txt"
        f.write_text("File content here", encoding="utf-8")
        result = _resolve_source([str(f), "Inline text here"])
        assert "File content" in result
        assert "Inline text" in result

    def test_nonexistent_path_treated_as_inline(self):
        result = _resolve_source("/nonexistent/path/that/doesnt/exist.txt")
        assert result == "/nonexistent/path/that/doesnt/exist.txt"


# ---------------------------------------------------------------------------
# _check_grounding
# ---------------------------------------------------------------------------


class TestCheckGrounding:
    def test_identical_text_high_score(self):
        text = "The AC-17 control requires multi-factor authentication."
        score = _check_grounding(text, text)
        assert score >= 0.95

    def test_paraphrased_text_moderate_score(self):
        source = "The AC-17 control requires multi-factor authentication for remote access."
        response = "AC-17 mandates that remote access uses multi-factor authentication."
        score = _check_grounding(response, source)
        assert score >= 0.5  # paraphrased but grounded

    def test_unrelated_text_low_score(self):
        source = "The AC-17 control requires multi-factor authentication."
        response = "Quantum computing leverages superposition and entanglement."
        score = _check_grounding(response, source)
        assert score < 0.5

    def test_empty_response_returns_zero(self):
        assert _check_grounding("", "some source") == 0.0

    def test_empty_source_returns_zero(self):
        assert _check_grounding("some response", "") == 0.0

    def test_score_range(self):
        score = _check_grounding("hello world", "hello world foo bar")
        assert 0.0 <= score <= 1.0

    def test_threshold_comparison(self):
        source = "Multi-factor authentication is required by AC-17."
        good = "AC-17 requires multi-factor authentication."
        bad = "The weather today is sunny and warm."
        assert _check_grounding(good, source, threshold=0.7) >= 0.7
        assert _check_grounding(bad, source, threshold=0.7) < 0.7

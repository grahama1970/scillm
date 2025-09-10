"""
Unit tests for experimental parallel acompletions functionality.
"""

import asyncio
import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from litellm import Router
from litellm.router_utils.parallel_acompletion import (
    RouterParallelRequest,
    RouterParallelResult,
    gather_parallel_acompletions,
    iter_parallel_acompletions,
)


class TestExperimentalFlags:
    """Test experimental flags module."""

    def test_parallel_acompletions_flag_default_false(self):
        """Test that the flag defaults to False."""
        with patch.dict(os.environ, {}, clear=True):
            # Re-import to get fresh value
            import importlib
            from litellm import experimental_flags
            importlib.reload(experimental_flags)
            assert experimental_flags.ENABLE_PARALLEL_ACOMPLETIONS is False

    def test_parallel_acompletions_flag_enabled(self):
        """Test that the flag can be enabled via environment variable."""
        with patch.dict(os.environ, {"LITELLM_ENABLE_PARALLEL_ACOMPLETIONS": "1"}):
            import importlib
            from litellm import experimental_flags
            importlib.reload(experimental_flags)
            assert experimental_flags.ENABLE_PARALLEL_ACOMPLETIONS is True

    def test_parallel_acompletions_flag_various_true_values(self):
        """Test various true values for the flag."""
        true_values = ["1", "true", "True", "TRUE", "yes", "YES", "on", "ON"]
        for value in true_values:
            with patch.dict(os.environ, {"LITELLM_ENABLE_PARALLEL_ACOMPLETIONS": value}):
                import importlib
                from litellm import experimental_flags
                importlib.reload(experimental_flags)
                assert experimental_flags.ENABLE_PARALLEL_ACOMPLETIONS is True, f"Failed for value: {value}"


class TestParallelAcompletionsRouter:
    """Test parallel acompletions methods on Router class."""

    def test_methods_exist_on_router(self):
        """Test that the new methods exist on Router instances."""
        router = Router()
        assert hasattr(router, "parallel_acompletions")
        assert hasattr(router, "iter_parallel_acompletions")
        assert callable(router.parallel_acompletions)
        assert callable(router.iter_parallel_acompletions)

    def test_parallel_acompletions_disabled_raises_error(self):
        """Test that methods raise RuntimeError when flag is disabled."""
        with patch("litellm.router.ENABLE_PARALLEL_ACOMPLETIONS", False):
            router = Router()
            
            with pytest.raises(RuntimeError, match="parallel_acompletions disabled"):
                asyncio.run(router.parallel_acompletions([]))
            
            with pytest.raises(RuntimeError, match="parallel_acompletions disabled"):
                for _ in router.iter_parallel_acompletions([]):
                    pass

    @pytest.mark.asyncio
    async def test_parallel_acompletions_enabled_empty_list(self):
        """Test that parallel_acompletions works with empty list when enabled."""
        with patch("litellm.router.ENABLE_PARALLEL_ACOMPLETIONS", True):
            router = Router()
            result = await router.parallel_acompletions([])
            assert result == []

    @pytest.mark.asyncio
    async def test_iter_parallel_acompletions_enabled_empty_list(self):
        """Test that iter_parallel_acompletions works with empty list when enabled."""
        with patch("litellm.router.ENABLE_PARALLEL_ACOMPLETIONS", True):
            router = Router()
            results = []
            async for item in router.iter_parallel_acompletions([]):
                results.append(item)
            assert results == []


class TestParallelAcompletionHelpers:
    """Test the helper functions in parallel_acompletion module."""

    @pytest.mark.asyncio
    async def test_gather_parallel_acompletions_empty_list(self):
        """Test gather_parallel_acompletions with empty request list."""
        router = MagicMock()
        result = await gather_parallel_acompletions(router, [])
        assert result == []

    @pytest.mark.asyncio
    async def test_iter_parallel_acompletions_empty_list(self):
        """Test iter_parallel_acompletions with empty request list."""
        router = MagicMock()
        results = []
        async for item in iter_parallel_acompletions(router, []):
            results.append(item)
        assert results == []

    @pytest.mark.asyncio
    async def test_gather_parallel_acompletions_mock_successful_requests(self):
        """Test gather_parallel_acompletions with mocked successful requests."""
        router = AsyncMock()
        mock_response = MagicMock()
        router.acompletion.return_value = mock_response
        
        requests = [
            {"model": "test-model", "messages": [{"role": "user", "content": "Hello"}]},
            {"model": "test-model", "messages": [{"role": "user", "content": "World"}]},
        ]
        
        results = await gather_parallel_acompletions(
            router, requests, concurrency=2, return_exceptions=True
        )
        
        assert len(results) == 2
        assert all(result["success"] for result in results)
        assert all(result["response"] == mock_response for result in results)
        assert router.acompletion.call_count == 2

    @pytest.mark.asyncio
    async def test_gather_parallel_acompletions_with_exception(self):
        """Test gather_parallel_acompletions with exceptions."""
        router = AsyncMock()
        router.acompletion.side_effect = Exception("Test error")
        
        requests = [
            {"model": "test-model", "messages": [{"role": "user", "content": "Hello"}]},
        ]
        
        results = await gather_parallel_acompletions(
            router, requests, concurrency=1, return_exceptions=True
        )
        
        assert len(results) == 1
        assert not results[0]["success"]
        assert results[0]["response"] is None
        assert isinstance(results[0]["exception"], Exception)
        assert str(results[0]["exception"]) == "Test error"

    @pytest.mark.asyncio
    async def test_iter_parallel_acompletions_mock_successful_requests(self):
        """Test iter_parallel_acompletions with mocked successful requests."""
        router = AsyncMock()
        mock_response = MagicMock()
        router.acompletion.return_value = mock_response
        
        requests = [
            {"model": "test-model", "messages": [{"role": "user", "content": "Hello"}]},
            {"model": "test-model", "messages": [{"role": "user", "content": "World"}]},
        ]
        
        results = []
        async for result in iter_parallel_acompletions(
            router, requests, concurrency=2, return_exceptions=True
        ):
            results.append(result)
        
        assert len(results) == 2
        assert all(result["success"] for result in results)
        assert all(result["response"] == mock_response for result in results)
        assert router.acompletion.call_count == 2
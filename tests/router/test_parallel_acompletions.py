"""
Tests for parallel async completion functionality.
"""
import sys
import os
import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

# Add the project root to the path
sys.path.insert(0, os.path.abspath("../.."))

from litellm import Router
from litellm.router_utils.parallel_acompletion import (
    ParallelAcompletionResult,
    ParallelAcompletionError,
    parallel_acompletion_wrapper,
    execute_parallel_acompletions,
    parallel_acompletion_fastest_response,
)
from litellm.experimental_flags import experimental_flags


@pytest.fixture
def model_list():
    """Fixture for router model list."""
    return [
        {
            "model_name": "gpt-3.5-turbo",
            "litellm_params": {
                "model": "gpt-3.5-turbo",
                "api_key": "test-key",
            },
        },
        {
            "model_name": "gpt-4",
            "litellm_params": {
                "model": "gpt-4",
                "api_key": "test-key",
            },
        },
        {
            "model_name": "claude-3-haiku",
            "litellm_params": {
                "model": "anthropic/claude-3-haiku-20240307",
                "api_key": "test-key",
            },
        },
    ]


@pytest.fixture
def sample_messages():
    """Fixture for sample messages."""
    return [{"role": "user", "content": "Hello, how are you?"}]


@pytest.fixture
def router(model_list):
    """Fixture for router instance."""
    return Router(model_list=model_list)


class TestParallelAcompletionResult:
    """Test ParallelAcompletionResult class."""

    def test_initialization(self):
        """Test result initialization."""
        result = ParallelAcompletionResult()
        assert result.responses == []
        assert result.successful_responses == []
        assert result.failed_responses == []
        assert result.total_requests == 0
        assert result.successful_count == 0
        assert result.failed_count == 0
        assert result.total_duration == 0.0

    def test_add_successful_response(self):
        """Test adding successful response."""
        result = ParallelAcompletionResult()
        mock_response = MagicMock()
        
        result.add_response(mock_response)
        
        assert len(result.responses) == 1
        assert len(result.successful_responses) == 1
        assert len(result.failed_responses) == 0
        assert result.successful_count == 1
        assert result.failed_count == 0

    def test_add_failed_response(self):
        """Test adding failed response."""
        result = ParallelAcompletionResult()
        error = Exception("Test error")
        
        result.add_response(error)
        
        assert len(result.responses) == 1
        assert len(result.successful_responses) == 0
        assert len(result.failed_responses) == 1
        assert result.successful_count == 0
        assert result.failed_count == 1

    def test_get_summary(self):
        """Test getting summary statistics."""
        result = ParallelAcompletionResult()
        result.total_requests = 3
        result.successful_count = 2
        result.failed_count = 1
        result.total_duration = 1.5
        
        summary = result.get_summary()
        
        assert summary['total_requests'] == 3
        assert summary['successful_count'] == 2
        assert summary['failed_count'] == 1
        assert summary['success_rate'] == 2/3
        assert summary['total_duration'] == 1.5
        assert summary['average_duration'] == 0.5


class TestExperimentalFlags:
    """Test experimental flags functionality."""

    def test_flag_defaults(self):
        """Test default flag values."""
        assert not experimental_flags.is_enabled('parallel_acompletions_enabled')
        assert experimental_flags.get('parallel_acompletions_max_concurrency') == 10

    def test_set_and_get_flags(self):
        """Test setting and getting flags."""
        experimental_flags.set('test_flag', True)
        assert experimental_flags.get('test_flag') is True
        assert experimental_flags.is_enabled('test_flag')

    def test_environment_variable_loading(self):
        """Test loading flags from environment variables."""
        with patch.dict(os.environ, {
            'LITELLM_EXPERIMENTAL_PARALLEL_ACOMPLETIONS': 'true',
            'LITELLM_EXPERIMENTAL_PARALLEL_ACOMPLETIONS_MAX_CONCURRENCY': '5'
        }):
            flags = experimental_flags.__class__()  # Create new instance
            assert flags.is_enabled('parallel_acompletions_enabled')
            assert flags.get('parallel_acompletions_max_concurrency') == 5


@pytest.mark.asyncio
class TestParallelAcompletionWrapper:
    """Test parallel_acompletion_wrapper function."""

    async def test_successful_wrapper(self, router, sample_messages):
        """Test wrapper with successful acompletion."""
        mock_response = MagicMock()
        router.acompletion = AsyncMock(return_value=mock_response)
        
        result = await parallel_acompletion_wrapper(
            router_instance=router,
            model="gpt-3.5-turbo",
            messages=sample_messages,
            request_id="test-1"
        )
        
        assert result == mock_response
        router.acompletion.assert_called_once_with(
            model="gpt-3.5-turbo",
            messages=sample_messages
        )

    async def test_wrapper_with_exception(self, router, sample_messages):
        """Test wrapper handles exceptions."""
        test_error = Exception("Test error")
        router.acompletion = AsyncMock(side_effect=test_error)
        
        result = await parallel_acompletion_wrapper(
            router_instance=router,
            model="gpt-3.5-turbo",
            messages=sample_messages,
            request_id="test-1"
        )
        
        assert result == test_error


@pytest.mark.asyncio
class TestExecuteParallelAcompletions:
    """Test execute_parallel_acompletions function."""

    async def test_successful_parallel_execution(self, router, sample_messages):
        """Test successful parallel execution."""
        mock_response_1 = MagicMock()
        mock_response_2 = MagicMock()
        
        # Mock the acompletion method to return different responses
        async def mock_acompletion(model=None, **kwargs):
            if model == "gpt-3.5-turbo":
                return mock_response_1
            elif model == "gpt-4":
                return mock_response_2
            else:
                raise Exception(f"Unknown model: {model}")
        
        router.acompletion = mock_acompletion
        
        models = ["gpt-3.5-turbo", "gpt-4"]
        messages_list = [sample_messages, sample_messages]
        
        result = await execute_parallel_acompletions(
            router_instance=router,
            models=models,
            messages_list=messages_list,
            max_concurrency=2
        )
        
        assert result.total_requests == 2
        assert result.successful_count == 2
        assert result.failed_count == 0
        assert len(result.successful_responses) == 2

    async def test_length_mismatch_error(self, router, sample_messages):
        """Test error when models and messages lists have different lengths."""
        models = ["gpt-3.5-turbo", "gpt-4"]
        messages_list = [sample_messages]  # Only one message list
        
        with pytest.raises(ParallelAcompletionError, match="Length mismatch"):
            await execute_parallel_acompletions(
                router_instance=router,
                models=models,
                messages_list=messages_list
            )

    async def test_mixed_success_and_failure(self, router, sample_messages):
        """Test parallel execution with mixed success and failure."""
        mock_response = MagicMock()
        test_error = Exception("Test error")
        
        # Mock the acompletion method with mixed results
        async def mock_acompletion(model=None, **kwargs):
            if model == "gpt-3.5-turbo":
                return mock_response
            elif model == "gpt-4":
                raise test_error
            else:
                raise Exception(f"Unknown model: {model}")
        
        router.acompletion = mock_acompletion
        
        models = ["gpt-3.5-turbo", "gpt-4"]
        messages_list = [sample_messages, sample_messages]
        
        result = await execute_parallel_acompletions(
            router_instance=router,
            models=models,
            messages_list=messages_list,
            max_concurrency=2
        )
        
        assert result.total_requests == 2
        assert result.successful_count == 1
        assert result.failed_count == 1
        assert len(result.successful_responses) == 1
        assert len(result.failed_responses) == 1


@pytest.mark.asyncio
class TestParallelAcompletionFastestResponse:
    """Test parallel_acompletion_fastest_response function."""

    async def test_fastest_response_success(self, router, sample_messages):
        """Test fastest response returns first successful result."""
        mock_response = MagicMock()
        
        # Mock slow and fast responses
        async def mock_acompletion(model=None, **kwargs):
            if model == "gpt-3.5-turbo":
                await asyncio.sleep(0.1)  # Slower
                return mock_response
            elif model == "gpt-4":
                await asyncio.sleep(0.01)  # Faster
                return mock_response
            else:
                raise Exception(f"Unknown model: {model}")
        
        router.acompletion = mock_acompletion
        
        models = ["gpt-3.5-turbo", "gpt-4"]
        
        result = await parallel_acompletion_fastest_response(
            router_instance=router,
            models=models,
            messages=sample_messages,
            max_concurrency=2
        )
        
        assert result == mock_response

    async def test_fastest_response_all_fail(self, router, sample_messages):
        """Test fastest response when all models fail."""
        test_error = Exception("Test error")
        
        # Mock all failing
        async def mock_acompletion(model=None, **kwargs):
            raise test_error
        
        router.acompletion = mock_acompletion
        
        models = ["gpt-3.5-turbo", "gpt-4"]
        
        result = await parallel_acompletion_fastest_response(
            router_instance=router,
            models=models,
            messages=sample_messages,
            max_concurrency=2
        )
        
        assert isinstance(result, Exception)

    async def test_fastest_response_no_models(self, router, sample_messages):
        """Test fastest response with no models."""
        with pytest.raises(ParallelAcompletionError, match="No models provided"):
            await parallel_acompletion_fastest_response(
                router_instance=router,
                models=[],
                messages=sample_messages
            )
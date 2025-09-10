"""
Parallel async completion utilities for LiteLLM Router.

This module provides functionality for running multiple async completion requests
in parallel with proper error handling and response aggregation.
"""
import asyncio
import time
import traceback
from typing import List, Dict, Any, Union, Optional, Tuple
from collections import defaultdict

import litellm
from litellm.types.utils import ModelResponse
from litellm.utils import CustomStreamWrapper
from litellm._logging import verbose_router_logger


class ParallelAcompletionError(Exception):
    """Exception raised during parallel acompletion operations."""
    pass


class ParallelAcompletionResult:
    """Container for parallel acompletion results."""
    
    def __init__(self):
        self.responses: List[Union[ModelResponse, Exception]] = []
        self.successful_responses: List[ModelResponse] = []
        self.failed_responses: List[Exception] = []
        self.total_requests: int = 0
        self.successful_count: int = 0
        self.failed_count: int = 0
        self.total_duration: float = 0.0

    def add_response(self, response: Union[ModelResponse, Exception]) -> None:
        """Add a response (success or error) to the result."""
        self.responses.append(response)
        if isinstance(response, Exception):
            self.failed_responses.append(response)
            self.failed_count += 1
        else:
            self.successful_responses.append(response)
            self.successful_count += 1

    def get_summary(self) -> Dict[str, Any]:
        """Get a summary of the parallel execution results."""
        return {
            'total_requests': self.total_requests,
            'successful_count': self.successful_count,
            'failed_count': self.failed_count,
            'success_rate': self.successful_count / max(self.total_requests, 1),
            'total_duration': self.total_duration,
            'average_duration': self.total_duration / max(self.total_requests, 1)
        }


async def parallel_acompletion_wrapper(
    router_instance,
    model: str,
    messages: List[Dict[str, str]],
    request_id: Optional[str] = None,
    **kwargs
) -> Union[ModelResponse, Exception]:
    """
    Wrapper for single acompletion call that handles exceptions gracefully.
    
    Args:
        router_instance: The Router instance
        model: Model name for completion
        messages: Messages for completion
        request_id: Optional request identifier for tracking
        **kwargs: Additional arguments for completion
        
    Returns:
        ModelResponse on success, Exception on failure
    """
    try:
        verbose_router_logger.debug(
            f"Starting parallel acompletion for model={model}, request_id={request_id}"
        )
        response = await router_instance.acompletion(
            model=model,
            messages=messages,
            **kwargs
        )
        verbose_router_logger.debug(
            f"Completed parallel acompletion for model={model}, request_id={request_id}"
        )
        return response
    except Exception as e:
        verbose_router_logger.error(
            f"Error in parallel acompletion for model={model}, request_id={request_id}: {str(e)}"
        )
        return e


async def execute_parallel_acompletions(
    router_instance,
    models: List[str],
    messages_list: List[List[Dict[str, str]]],
    max_concurrency: Optional[int] = None,
    **kwargs
) -> ParallelAcompletionResult:
    """
    Execute multiple acompletion requests in parallel.
    
    Args:
        router_instance: The Router instance
        models: List of model names
        messages_list: List of message lists for each request
        max_concurrency: Maximum number of concurrent requests
        **kwargs: Additional arguments passed to each completion
        
    Returns:
        ParallelAcompletionResult containing all responses and statistics
    """
    if len(models) != len(messages_list):
        raise ParallelAcompletionError(
            f"Length mismatch: {len(models)} models vs {len(messages_list)} message lists"
        )
    
    if max_concurrency is None:
        from litellm.experimental_flags import experimental_flags
        max_concurrency = experimental_flags.get(
            'parallel_acompletions_max_concurrency', 10
        )
    
    result = ParallelAcompletionResult()
    result.total_requests = len(models)
    
    start_time = time.time()
    
    # Create semaphore to limit concurrency
    semaphore = asyncio.Semaphore(max_concurrency)
    
    async def _semaphore_wrapper(model: str, messages: List[Dict[str, str]], idx: int):
        """Wrapper that uses semaphore to limit concurrency."""
        async with semaphore:
            return await parallel_acompletion_wrapper(
                router_instance=router_instance,
                model=model,
                messages=messages,
                request_id=f"parallel_{idx}",
                **kwargs
            )
    
    # Create tasks for all requests
    tasks = []
    for i, (model, messages) in enumerate(zip(models, messages_list)):
        task = _semaphore_wrapper(model, messages, i)
        tasks.append(task)
    
    verbose_router_logger.info(
        f"Starting {len(tasks)} parallel acompletion requests with max_concurrency={max_concurrency}"
    )
    
    # Execute all tasks concurrently
    responses = await asyncio.gather(*tasks, return_exceptions=True)
    
    end_time = time.time()
    result.total_duration = end_time - start_time
    
    # Process responses
    for response in responses:
        result.add_response(response)
    
    verbose_router_logger.info(
        f"Completed parallel acompletions: {result.get_summary()}"
    )
    
    return result


async def parallel_acompletion_fastest_response(
    router_instance,
    models: List[str],
    messages: List[Dict[str, str]],
    max_concurrency: Optional[int] = None,
    **kwargs
) -> Union[ModelResponse, Exception]:
    """
    Execute the same request against multiple models and return the fastest response.
    
    Args:
        router_instance: The Router instance
        models: List of model names to try
        messages: Messages for completion (same for all models)
        max_concurrency: Maximum number of concurrent requests
        **kwargs: Additional arguments passed to each completion
        
    Returns:
        The first successful ModelResponse, or the last exception if all fail
    """
    if not models:
        raise ParallelAcompletionError("No models provided")
    
    if max_concurrency is None:
        from litellm.experimental_flags import experimental_flags
        max_concurrency = experimental_flags.get(
            'parallel_acompletions_max_concurrency', len(models)
        )
    
    # Create semaphore to limit concurrency
    semaphore = asyncio.Semaphore(max_concurrency)
    
    async def _semaphore_wrapper(model: str, idx: int):
        """Wrapper that uses semaphore to limit concurrency."""
        async with semaphore:
            return await parallel_acompletion_wrapper(
                router_instance=router_instance,
                model=model,
                messages=messages,
                request_id=f"fastest_{idx}",
                **kwargs
            )
    
    # Create tasks and track them properly
    task_list = [asyncio.create_task(_semaphore_wrapper(model, i)) for i, model in enumerate(models)]
    
    verbose_router_logger.info(
        f"Starting parallel acompletion race with {len(models)} models"
    )
    
    # Use asyncio.as_completed to return the first successful response
    last_exception = None
    
    for coro in asyncio.as_completed(task_list):
        try:
            response = await coro
            if not isinstance(response, Exception):
                verbose_router_logger.info(
                    f"Fastest response received from parallel acompletion race"
                )
                # Cancel remaining tasks
                for task in task_list:
                    if not task.done():
                        task.cancel()
                return response
            else:
                last_exception = response
                verbose_router_logger.warning(
                    f"Failed response in parallel acompletion race: {str(response)}"
                )
        except Exception as e:
            last_exception = e
            verbose_router_logger.warning(
                f"Exception in parallel acompletion race: {str(e)}"
            )
    
    # If we get here, all requests failed
    verbose_router_logger.error(
        f"All models failed in parallel acompletion race"
    )
    return last_exception or ParallelAcompletionError("All parallel requests failed")
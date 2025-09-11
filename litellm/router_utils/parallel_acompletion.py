"""
Experimental parallel acompletions utilities for LiteLLM Router.

This module provides functions and types for running multiple acompletion calls
concurrently using the Router class.
"""

import asyncio
import traceback
from typing import Any, AsyncGenerator, Dict, List, Optional, Union
from typing_extensions import TypedDict

from litellm.types.utils import ModelResponse


class RouterParallelRequest(TypedDict):
    """
    Request object for parallel acompletions.
    
    At minimum, must contain 'model' and 'messages' keys.
    Additional parameters can be passed as key-value pairs.
    """
    model: str
    messages: List[Dict[str, Any]]
    # Additional optional parameters can be passed


class RouterParallelResult(TypedDict):
    """
    Result object from parallel acompletions.
    
    Contains either a successful response or an exception.
    """
    success: bool
    response: Optional[ModelResponse]
    exception: Optional[Exception]
    request_index: Optional[int]  # Original index in the request list


async def _run_single_acompletion(
    router_instance: Any,
    request: RouterParallelRequest,
    request_index: int,
    return_exceptions: bool = True,
) -> RouterParallelResult:
    """
    Run a single acompletion call and return the result.
    
    Args:
        router_instance: The Router instance to use
        request: The request parameters
        request_index: Original index of this request in the input list
        return_exceptions: Whether to return exceptions as results instead of raising
        
    Returns:
        RouterParallelResult with the completion result or exception
    """
    try:
        # Extract model and messages, rest are kwargs
        request_copy = dict(request)
        model = request_copy.pop("model")
        messages = request_copy.pop("messages")
        
        # Call acompletion with the remaining parameters
        response = await router_instance.acompletion(
            model=model,
            messages=messages,
            **request_copy
        )
        
        return RouterParallelResult(
            success=True,
            response=response,
            exception=None,
            request_index=request_index,
        )
    except Exception as e:
        if return_exceptions:
            return RouterParallelResult(
                success=False,
                response=None,
                exception=e,
                request_index=request_index,
            )
        else:
            raise


async def gather_parallel_acompletions(
    router_instance: Any,
    requests: List[RouterParallelRequest],
    *,
    concurrency: int = 8,
    return_exceptions: bool = True,
    preserve_order: bool = False,
) -> List[RouterParallelResult]:
    """
    Run multiple acompletion calls concurrently and collect all results.
    
    Args:
        router_instance: The Router instance to use for acompletions
        requests: List of request dictionaries
        concurrency: Maximum number of concurrent acompletion calls
        return_exceptions: Whether to capture exceptions as results instead of failing fast
        preserve_order: Whether to reorder results to match input sequence
        
    Returns:
        List of RouterParallelResult objects
    """
    if not requests:
        return []
    
    # Create semaphore to control concurrency
    semaphore = asyncio.Semaphore(concurrency)
    
    async def run_with_semaphore(request: RouterParallelRequest, index: int) -> RouterParallelResult:
        async with semaphore:
            return await _run_single_acompletion(
                router_instance=router_instance,
                request=request,
                request_index=index,
                return_exceptions=return_exceptions,
            )
    
    # Create tasks for all requests
    tasks = [
        run_with_semaphore(request, index)
        for index, request in enumerate(requests)
    ]
    
    # Execute all tasks concurrently
    results = await asyncio.gather(*tasks, return_exceptions=return_exceptions)
    
    # If preserve_order is True, sort results by request_index
    if preserve_order:
        results = sorted(results, key=lambda r: r.get("request_index", 0) if isinstance(r, dict) else 0)
    
    return results


async def iter_parallel_acompletions(
    router_instance: Any,
    requests: List[RouterParallelRequest],
    *,
    concurrency: int = 8,
    return_exceptions: bool = True,
) -> AsyncGenerator[RouterParallelResult, None]:
    """
    Async iterator yielding each acompletion result as soon as it finishes.
    Results are yielded in completion order, not input order.
    
    Args:
        router_instance: The Router instance to use for acompletions
        requests: List of request dictionaries
        concurrency: Maximum number of concurrent acompletion calls
        return_exceptions: Whether to capture exceptions as results instead of failing fast
        
    Yields:
        RouterParallelResult objects as they complete
    """
    if not requests:
        return
    
    # Create semaphore to control concurrency
    semaphore = asyncio.Semaphore(concurrency)
    
    async def run_with_semaphore(request: RouterParallelRequest, index: int) -> RouterParallelResult:
        async with semaphore:
            return await _run_single_acompletion(
                router_instance=router_instance,
                request=request,
                request_index=index,
                return_exceptions=return_exceptions,
            )
    
    # Create tasks for all requests
    tasks = [
        asyncio.create_task(run_with_semaphore(request, index))
        for index, request in enumerate(requests)
    ]
    
    # Use asyncio.as_completed to yield results as they finish
    for completed_task in asyncio.as_completed(tasks):
        try:
            result = await completed_task
            yield result
        except Exception as e:
            if return_exceptions:
                # Create an error result if the task itself failed
                yield RouterParallelResult(
                    success=False,
                    response=None,
                    exception=e,
                    request_index=None,
                )
            else:
                raise
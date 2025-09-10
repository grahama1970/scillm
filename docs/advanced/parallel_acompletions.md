# Parallel Async Completions

LiteLLM's Router supports parallel async completion requests, allowing you to execute multiple completion calls simultaneously for improved performance and throughput.

## Overview

The parallel async completions feature provides two main use cases:

1. **Multiple Models, Different Requests**: Execute different completion requests across multiple models simultaneously
2. **Multiple Models, Same Request**: Send the same request to multiple models and get the fastest response

## Setup

### Enable Experimental Feature

This feature is currently experimental. Enable it using environment variables:

```bash
export LITELLM_EXPERIMENTAL_PARALLEL_ACOMPLETIONS=true
export LITELLM_EXPERIMENTAL_PARALLEL_ACOMPLETIONS_MAX_CONCURRENCY=10
```

Or programmatically:

```python
from litellm.experimental_flags import experimental_flags

experimental_flags.set('parallel_acompletions_enabled', True)
experimental_flags.set('parallel_acompletions_max_concurrency', 10)
```

### Router Configuration

```python
from litellm import Router

router = Router(model_list=[
    {
        "model_name": "gpt-3.5-turbo",
        "litellm_params": {
            "model": "gpt-3.5-turbo",
            "api_key": "your-openai-key"
        }
    },
    {
        "model_name": "gpt-4",
        "litellm_params": {
            "model": "gpt-4", 
            "api_key": "your-openai-key"
        }
    },
    {
        "model_name": "claude-3-haiku",
        "litellm_params": {
            "model": "claude-3-haiku",
            "api_key": "your-anthropic-key"
        }
    }
])
```

## Usage Examples

### Parallel Execution of Different Requests

Execute different completion requests across multiple models simultaneously:

```python
import asyncio
from litellm import Router

async def main():
    # Define your models and different requests
    models = ["gpt-3.5-turbo", "gpt-4", "claude-3-haiku"]
    messages_list = [
        [{"role": "user", "content": "Explain quantum computing"}],
        [{"role": "user", "content": "Write a Python function to sort a list"}],
        [{"role": "user", "content": "What is the capital of France?"}]
    ]
    
    # Execute in parallel
    result = await router.parallel_acompletions(
        models=models,
        messages_list=messages_list,
        max_concurrency=3
    )
    
    # Process results
    print(f"Total requests: {result.total_requests}")
    print(f"Successful: {result.successful_count}")
    print(f"Failed: {result.failed_count}")
    print(f"Duration: {result.total_duration:.2f}s")
    
    # Access individual responses
    for i, response in enumerate(result.successful_responses):
        print(f"Response {i}: {response.choices[0].message.content}")

asyncio.run(main())
```

### Fastest Response from Multiple Models

Send the same request to multiple models and get the fastest response:

```python
import asyncio
from litellm import Router

async def main():
    models = ["gpt-3.5-turbo", "gpt-4", "claude-3-haiku"]
    messages = [{"role": "user", "content": "What is machine learning?"}]
    
    # Get fastest response
    response = await router.parallel_acompletion_fastest(
        models=models,
        messages=messages,
        max_concurrency=3
    )
    
    if isinstance(response, Exception):
        print(f"All models failed: {response}")
    else:
        print(f"Fastest response: {response.choices[0].message.content}")
        print(f"Model used: {response.model}")

asyncio.run(main())
```

## Configuration Options

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `LITELLM_EXPERIMENTAL_PARALLEL_ACOMPLETIONS` | Enable parallel acompletions | `false` |
| `LITELLM_EXPERIMENTAL_PARALLEL_ACOMPLETIONS_MAX_CONCURRENCY` | Maximum concurrent requests | `10` |

### Function Parameters

#### `parallel_acompletions()`

- `models`: List of model names to use
- `messages_list`: List of message lists for each request
- `max_concurrency`: Maximum number of concurrent requests (optional)
- `**kwargs`: Additional parameters passed to each completion call

#### `parallel_acompletion_fastest()`

- `models`: List of model names to try
- `messages`: Messages for completion (same for all models)
- `max_concurrency`: Maximum number of concurrent requests (optional)
- `**kwargs`: Additional parameters passed to each completion call

## Response Format

### ParallelAcompletionResult

The `parallel_acompletions()` method returns a `ParallelAcompletionResult` object with:

```python
class ParallelAcompletionResult:
    responses: List[Union[ModelResponse, Exception]]  # All responses
    successful_responses: List[ModelResponse]         # Only successful responses  
    failed_responses: List[Exception]                 # Only failed responses
    total_requests: int                               # Total number of requests
    successful_count: int                             # Number of successful requests
    failed_count: int                                 # Number of failed requests
    total_duration: float                             # Total execution time
    
    def get_summary(self) -> Dict[str, Any]:
        # Returns summary statistics including success rate
```

## Error Handling

The parallel acompletion functions handle errors gracefully:

- Individual request failures don't stop other requests
- Failed requests are captured as exceptions in the result
- The fastest response method returns the last exception if all requests fail

```python
result = await router.parallel_acompletions(
    models=["gpt-3.5-turbo", "invalid-model"],
    messages_list=[messages1, messages2]
)

# Check for failures
if result.failed_count > 0:
    print(f"Some requests failed: {result.failed_count}/{result.total_requests}")
    for error in result.failed_responses:
        print(f"Error: {error}")
```

## Performance Considerations

### Concurrency Limits

- Default maximum concurrency is 10 requests
- Adjust based on your API rate limits and system resources
- Higher concurrency may lead to rate limiting from providers

### Use Cases

**Best for:**
- Comparing responses from multiple models
- High-throughput scenarios with many independent requests
- Reducing latency by racing multiple models

**Avoid when:**
- Making requests that depend on each other
- Working with very large context windows (may hit memory limits)
- Provider rate limits are very restrictive

## Monitoring and Debugging

Enable debug logging to monitor parallel execution:

```python
import litellm
litellm.set_verbose = True

# Or use the router logger
from litellm._logging import verbose_router_logger
import logging
verbose_router_logger.setLevel(logging.DEBUG)
```

The logs will show:
- Start/completion of individual requests
- Concurrency management
- Error details for failed requests
- Summary statistics

## Integration with Existing Features

Parallel acompletions work with existing Router features:

- **Fallbacks**: Each model call uses the router's fallback system
- **Caching**: Responses are cached according to your cache configuration
- **Load Balancing**: Model selection follows your routing strategy
- **Logging**: All requests are logged through the standard logging system

## Limitations

- Currently experimental and subject to change
- Streaming responses are not supported in parallel mode
- Function calling may have limited support depending on models
- Memory usage scales with concurrency level
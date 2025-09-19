"""
Utility functions for cleaning up async HTTP clients to prevent resource leaks.
"""
import asyncio


async def close_litellm_async_clients():
    """
    Close all cached async HTTP clients to prevent resource leaks.

    This function iterates through all cached clients in litellm's in-memory cache
    and closes any aiohttp client sessions that are still open.
    """
    # Import here to avoid circular import
    import litellm
    from litellm.llms.custom_httpx.aiohttp_handler import BaseLLMAIOHTTPHandler

    cache_dict = getattr(litellm.in_memory_llm_clients_cache, "cache_dict", {})

    for key, handler in cache_dict.items():
        # Handle BaseLLMAIOHTTPHandler instances (aiohttp_openai provider)
        if isinstance(handler, BaseLLMAIOHTTPHandler) and hasattr(handler, "close"):
            try:
                await handler.close()
            except Exception:
                # Silently ignore errors during cleanup (log at DEBUG for triage)
                import logging
                logging.getLogger("litellm").debug("async_client_cleanup: aiohttp handler close() failed", exc_info=True)

        # Handle AsyncHTTPHandler instances (used by Gemini and other providers)
        elif hasattr(handler, "client"):
            client = handler.client
            # Check if the httpx client has an aiohttp transport
            if hasattr(client, "_transport") and hasattr(client._transport, "aclose"):
                try:
                    await client._transport.aclose()
                except Exception:
                    import logging
                    logging.getLogger("litellm").debug("async_client_cleanup: transport aclose() failed", exc_info=True)
            # Also close the httpx client itself
            if hasattr(client, "aclose") and not client.is_closed:
                try:
                    await client.aclose()
                except Exception:
                    import logging
                    logging.getLogger("litellm").debug("async_client_cleanup: client aclose() failed", exc_info=True)

        # Handle any other objects with aclose method
        elif hasattr(handler, "aclose"):
            try:
                await handler.aclose()
            except Exception:
                import logging
                logging.getLogger("litellm").debug("async_client_cleanup: generic aclose() failed", exc_info=True)


def register_async_client_cleanup():
    """
    Register the async client cleanup function to run at exit.

    This ensures that all async HTTP clients are properly closed when the program exits.
    """
    import atexit

    def cleanup_wrapper():
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # Schedule the cleanup coroutine
                loop.create_task(close_litellm_async_clients())
            else:
                # Run the cleanup coroutine
                loop.run_until_complete(close_litellm_async_clients())
        except Exception:
            # If we can't get an event loop or it's already closed, try creating a new one
            try:
                loop = asyncio.new_event_loop()
                loop.run_until_complete(close_litellm_async_clients())
                loop.close()
            except Exception:
                import logging
                logging.getLogger("litellm").debug("async_client_cleanup: cleanup_wrapper failed", exc_info=True)

    atexit.register(cleanup_wrapper)

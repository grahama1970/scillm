"""
Experimental flags for LiteLLM features.

This module contains flags for enabling experimental features in LiteLLM.
"""

import os
from typing import Optional


def _get_env_bool(env_var: str, default: bool = False) -> bool:
    """
    Get a boolean value from an environment variable.
    
    Args:
        env_var: The environment variable name
        default: Default value if the environment variable is not set or invalid
        
    Returns:
        Boolean value from the environment variable or default
    """
    value: Optional[str] = os.getenv(env_var)
    if value is None:
        return default
    
    return value.lower() in ('1', 'true', 'yes', 'on')


# Experimental parallel acompletions feature flag
# Controlled by LITELLM_ENABLE_PARALLEL_ACOMPLETIONS environment variable
ENABLE_PARALLEL_ACOMPLETIONS: bool = _get_env_bool("LITELLM_ENABLE_PARALLEL_ACOMPLETIONS", default=False)
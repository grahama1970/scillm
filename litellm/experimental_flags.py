"""
Experimental flags for LiteLLM features.

This module contains feature flags for experimental functionality that may be
subject to change or deprecation in future versions.
"""
import os
from typing import Dict, Any


class ExperimentalFlags:
    """Container for experimental feature flags."""

    def __init__(self):
        self.flags: Dict[str, Any] = {}
        self._load_from_environment()

    def _load_from_environment(self) -> None:
        """Load experimental flags from environment variables."""
        # Load parallel acompletion flags
        self.flags['parallel_acompletions_enabled'] = self._get_bool_env(
            'LITELLM_EXPERIMENTAL_PARALLEL_ACOMPLETIONS', False
        )
        self.flags['parallel_acompletions_max_concurrency'] = self._get_int_env(
            'LITELLM_EXPERIMENTAL_PARALLEL_ACOMPLETIONS_MAX_CONCURRENCY', 10
        )

    def _get_bool_env(self, key: str, default: bool = False) -> bool:
        """Get boolean value from environment variable."""
        value = os.getenv(key, '').lower()
        if value in ('true', '1', 'yes', 'on'):
            return True
        elif value in ('false', '0', 'no', 'off'):
            return False
        return default

    def _get_int_env(self, key: str, default: int = 0) -> int:
        """Get integer value from environment variable."""
        try:
            return int(os.getenv(key, str(default)))
        except (ValueError, TypeError):
            return default

    def get(self, flag_name: str, default: Any = None) -> Any:
        """Get a flag value by name."""
        return self.flags.get(flag_name, default)

    def set(self, flag_name: str, value: Any) -> None:
        """Set a flag value."""
        self.flags[flag_name] = value

    def is_enabled(self, flag_name: str) -> bool:
        """Check if a flag is enabled (True)."""
        return bool(self.get(flag_name, False))


# Global instance
experimental_flags = ExperimentalFlags()
from __future__ import annotations

"""
Experimental provider placeholder for "codex-cloud".

We reserve the model prefix ("codex-cloud/<id>") so that get_llm_provider() resolves
to this provider, but we intentionally raise a clear error if someone calls it via
completion(). Use scillm.extras.codex_cloud helpers instead.
"""

from typing import Any, Optional, Union, Callable
from litellm.llms.custom_llm import CustomLLM, CustomLLMError
from litellm.utils import ModelResponse


class CodexCloudLLM(CustomLLM):
    def completion(
        self,
        model: str,
        messages: list,
        api_base: Optional[str],
        custom_prompt_dict: dict,
        model_response: ModelResponse,
        print_verbose: Callable,
        encoding,
        api_key,
        logging_obj,
        optional_params: dict,
        acompletion=None,
        litellm_params=None,
        logger_fn=None,
        headers: dict = {},
        timeout: Optional[Union[float, Any]] = None,
        client: Optional[Any] = None,
    ) -> ModelResponse:
        raise CustomLLMError(
            status_code=400,
            message=(
                "codex-cloud chat/completions not implemented. "
                "Use scillm.extras.codex_cloud.generate_variants_cloud() for Option A (best-of-N)."
            ),
        )


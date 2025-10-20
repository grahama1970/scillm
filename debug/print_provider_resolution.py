import os
from litellm.litellm_core_utils.get_llm_provider_logic import get_llm_provider

def main():
    model = os.environ.get("MODEL", "codex-agent/gpt-5")
    base = os.environ.get("BASE", "http://127.0.0.1:8089")
    m, prov, key, abase = get_llm_provider(model=model, api_base=base)
    print({"model_in": model, "resolved_model": m, "provider": prov, "api_base": abase})

if __name__ == "__main__":
    main()


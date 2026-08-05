from scillm.proxy.providers.claude import CLAUDE_MODEL_MAP


def test_claude_model_aliases_use_live_oauth_ids():
    assert CLAUDE_MODEL_MAP["claude-sonnet-5"] == "claude-sonnet-5"
    assert CLAUDE_MODEL_MAP["claude-fable-5"] == "claude-fable-5"
    assert CLAUDE_MODEL_MAP["claude-sonnet-4-6"] == "claude-sonnet-4-6"
    assert CLAUDE_MODEL_MAP["claude-opus-4-8"] == "claude-opus-4-8"
    assert CLAUDE_MODEL_MAP["claude-opus-4-7"] == "claude-opus-4-7"
    assert CLAUDE_MODEL_MAP["claude-opus-4-6"] == "claude-opus-4-6"
    assert CLAUDE_MODEL_MAP["claude-opus-4-5"] == "claude-opus-4-5-20251101"
    assert CLAUDE_MODEL_MAP["claude-haiku-4-5"] == "claude-haiku-4-5-20251001"
    assert CLAUDE_MODEL_MAP["claude-sonnet-4-5"] == "claude-sonnet-4-5-20250929"

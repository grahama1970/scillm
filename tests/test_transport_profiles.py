"""Deterministic tests for the scillm.transport_profile.v1 registry (issue #27)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import scillm.proxy.transport_profiles as tp
from scillm.proxy.errors import ProxyError
from scillm.proxy.transport_profiles import (
    ProfileRegistry,
    TransportProfile,
    build_default_profiles,
    create_transport_profiles_router,
    readiness_record,
)


def make_profile(**overrides):
    base = dict(
        id="p1",
        label="Test",
        provider="ollama",
        model="local-text",
        auth_source="none",
        mode="model_turn",
        capabilities=["streaming", "cancellation", "structured_events"],
    )
    base.update(overrides)
    return TransportProfile(**base)


class TestProfileValidation:
    def test_valid_profile_roundtrips(self):
        p = make_profile()
        assert p.model_dump(by_alias=True)["schema"] == tp.PROFILE_SCHEMA

    def test_unknown_mode_rejected(self):
        with pytest.raises(ValueError, match="unknown transport mode"):
            make_profile(mode="agent_harness")

    def test_unknown_capability_rejected(self):
        with pytest.raises(ValueError, match="unknown transport capability"):
            make_profile(capabilities=["teleport"])

    def test_tau_owned_capability_rejected(self):
        for cap in ("tool_execution", "node_completion", "evidence_acceptance"):
            with pytest.raises(ValueError, match="Tau-owned"):
                make_profile(capabilities=[cap])

    def test_opaque_compat_cannot_claim_native_turn_caps(self):
        with pytest.raises(ValueError, match="opaque_agent_compat"):
            make_profile(mode="opaque_agent_compat", capabilities=["tool_calling"])

    def test_wrong_schema_rejected(self):
        with pytest.raises(ValueError, match="unsupported profile schema"):
            TransportProfile(**{**make_profile().model_dump(by_alias=True), "schema": "v0"})

    def test_unknown_tag_rejected(self):
        with pytest.raises(ValueError, match="unknown semantic tag"):
            make_profile(tags=["wizardry"])


class TestRegistry:
    def test_unknown_fallback_fails_closed(self):
        with pytest.raises(ValueError, match="unknown fallback"):
            ProfileRegistry([make_profile(fallbacks=["ghost"])], {})

    def test_duplicate_id_fails_closed(self):
        with pytest.raises(ValueError, match="duplicate profile id"):
            ProfileRegistry([make_profile(), make_profile()], {})

    def test_alias_to_unknown_profile_fails_closed(self):
        with pytest.raises(ValueError, match="unknown profile"):
            ProfileRegistry([make_profile()], {"backend": "ghost"})

    def test_unknown_profile_lookup_is_404(self):
        reg = ProfileRegistry([make_profile()], {})
        with pytest.raises(ProxyError) as ei:
            reg.get("nope")
        assert ei.value.status_code == 404
        assert ei.value.error_type == "unknown_transport_profile"

    def test_resolve_walks_ordered_fallbacks_without_silent_downgrade(self):
        strong = make_profile(id="strong", capabilities=["streaming", "tool_calling", "cancellation", "structured_events"])
        weak = make_profile(id="weak", capabilities=["streaming"], fallbacks=["strong"])
        reg = ProfileRegistry([strong, weak], {})
        # weak lacks tool_calling → resolution moves to its declared fallback
        assert reg.resolve("weak", ["tool_calling"]).id == "strong"
        # nothing in the chain has 'files' → fail closed, never downgrade
        with pytest.raises(ProxyError) as ei:
            reg.resolve("weak", ["files"])
        assert ei.value.error_type == "transport_capability_unsatisfied"
        assert ei.value.details["skipped"]

    def test_resolve_rejects_unknown_capability(self):
        reg = ProfileRegistry([make_profile()], {})
        with pytest.raises(ProxyError) as ei:
            reg.resolve("p1", ["telepathy"])
        assert ei.value.error_type == "unknown_transport_capability"

    def test_fallback_outside_declared_chain_is_never_used(self):
        other = make_profile(id="other", capabilities=list(tp.TRANSPORT_CAPABILITIES))
        head = make_profile(id="head", capabilities=["streaming"], fallbacks=[])
        reg = ProfileRegistry([head, other], {})
        with pytest.raises(ProxyError):
            reg.resolve("head", ["tool_calling"])  # 'other' satisfies but is not declared


class TestDefaults:
    def test_default_registry_builds_and_has_role_aliases(self):
        config = SimpleNamespace(
            chutes_api_base="https://llm.chutes.ai/v1",
            gemini_api_base="https://gemini",
            ollama_api_base="http://localhost:11434",
            model_groups={"text": object(), "vlm": object()},
        )
        profiles, aliases = build_default_profiles(config)
        reg = ProfileRegistry(profiles, aliases)
        for role in ("coordinator", "backend", "frontend", "documentation", "testing", "independent-review"):
            assert reg.get(role)  # alias resolves
        compat = reg.get("opencode-serve-compat")
        assert compat.mode == "opaque_agent_compat"
        assert "tool_calling" not in compat.capabilities


class TestReadiness:
    @pytest.mark.asyncio
    async def test_config_presence_is_not_live_readiness(self):
        record = await readiness_record(make_profile(), live_probe=None)
        assert record["state"] == "credential_ready"
        assert record["state"] != "transport_live_ready"

    @pytest.mark.asyncio
    async def test_missing_credential_is_unavailable(self, monkeypatch):
        monkeypatch.delenv("CHUTES_API_KEY", raising=False)
        profile = make_profile(provider="chutes", capabilities=["streaming"])
        record = await readiness_record(profile)
        assert record["state"] == "unavailable"
        assert "CHUTES_API_KEY" in record["reason"]

    @pytest.mark.asyncio
    async def test_live_probe_success_yields_live_ready_with_evidence(self):
        async def probe(profile):
            return {"resolved_model": "qwen2.5:0.5b", "upstream_id": "chatcmpl-xyz"}

        record = await readiness_record(make_profile(), live_probe=probe)
        assert record["state"] == "transport_live_ready"
        assert record["evidence"]["live_probe"]["upstream_id"] == "chatcmpl-xyz"

    @pytest.mark.asyncio
    async def test_live_probe_failure_is_degraded_with_reason(self):
        async def probe(profile):
            raise RuntimeError("upstream 503")

        record = await readiness_record(make_profile(), live_probe=probe)
        assert record["state"] == "degraded"
        assert "upstream 503" in record["reason"]


@pytest.fixture()
def client(monkeypatch):
    strong = make_profile(id="strong", capabilities=["streaming", "tool_calling", "cancellation", "structured_events"])
    weak = make_profile(id="weak", capabilities=["streaming"], fallbacks=["strong"])
    reg = ProfileRegistry([strong, weak], {"backend": "strong"})
    monkeypatch.setattr(tp, "_registry", reg)

    async def probe(profile):
        return {"resolved_model": profile.model, "upstream_id": "chatcmpl-live"}

    app = FastAPI()
    app.include_router(create_transport_profiles_router(lambda r: None, live_probe=probe), prefix="/v1/scillm")

    @app.exception_handler(ProxyError)
    async def _handler(request, exc):
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=exc.status_code, content={"error": {"message": exc.message, "type": exc.error_type}})

    return TestClient(app)


class TestRoutes:
    def test_list_profiles(self, client):
        r = client.get("/v1/scillm/profiles")
        assert r.status_code == 200
        body = r.json()
        assert {p["id"] for p in body["profiles"]} == {"strong", "weak"}
        assert body["aliases"]["backend"] == "strong"

    def test_capability_filter(self, client):
        r = client.get("/v1/scillm/profiles/capabilities", params={"require": "tool_calling"})
        rows = {row["profile"]: row for row in r.json()["profiles"]}
        assert rows["strong"]["satisfies"] is True
        assert rows["weak"]["satisfies"] is False
        assert rows["weak"]["missing"] == ["tool_calling"]

    def test_capability_filter_unknown_cap_fails_closed(self, client):
        r = client.get("/v1/scillm/profiles/capabilities", params={"require": "telepathy"})
        assert r.status_code == 422

    def test_readiness_unknown_profile_404(self, client):
        r = client.get("/v1/scillm/profiles/readiness", params={"profile": "ghost"})
        assert r.status_code == 404

    def test_readiness_live_readback(self, client):
        r = client.get("/v1/scillm/profiles/readiness", params={"profile": "strong", "live": "true"})
        rec = r.json()["readiness"][0]
        assert rec["state"] == "transport_live_ready"
        assert rec["evidence"]["live_probe"]["upstream_id"] == "chatcmpl-live"

    def test_readiness_without_live_never_claims_live(self, client):
        r = client.get("/v1/scillm/profiles/readiness")
        assert all(rec["state"] != "transport_live_ready" for rec in r.json()["readiness"])


class TestDynamicModelCheck:
    @pytest.mark.asyncio
    async def test_unknown_model_degrades_readiness_with_suggestions(self, monkeypatch):
        async def fake_ids():
            return ["claude-fable-5", "claude-sonnet-4-6", "local-text", "ollama:*"]

        monkeypatch.setattr(tp, "_known_model_ids", fake_ids)
        bad = make_profile(model="claude-fable-6")  # wrong model, as in a mistyped DAG
        record = await tp.readiness_record(bad)
        assert record["state"] == "degraded"
        assert "claude-fable-5" in record["reason"]
        assert record["evidence"]["model_check"]["known"] is False
        assert "claude-fable-5" in record["evidence"]["model_check"]["suggestions"]

    @pytest.mark.asyncio
    async def test_known_model_passes_check(self, monkeypatch):
        async def fake_ids():
            return ["local-text"]

        monkeypatch.setattr(tp, "_known_model_ids", fake_ids)
        record = await tp.readiness_record(make_profile(model="local-text"))
        assert record["state"] == "credential_ready"
        assert record["evidence"]["model_check"] == {"known": True, "checked": True}

    @pytest.mark.asyncio
    async def test_wildcard_routed_model_passes(self, monkeypatch):
        async def fake_ids():
            return ["ollama:*"]

        monkeypatch.setattr(tp, "_known_model_ids", fake_ids)
        record = await tp.readiness_record(make_profile(model="ollama:qwen2.5:0.5b"))
        assert record["evidence"]["model_check"]["known"] is True

    @pytest.mark.asyncio
    async def test_listing_unavailable_does_not_block(self, monkeypatch):
        async def fake_ids():
            raise RuntimeError("proxy not up")

        monkeypatch.setattr(tp, "_known_model_ids", fake_ids)
        record = await tp.readiness_record(make_profile())
        assert record["state"] == "credential_ready"
        assert record["evidence"]["model_check"]["checked"] is False


def test_fable_profile_registered_as_coordinator():
    profiles, aliases = build_default_profiles(SimpleNamespace())
    reg = ProfileRegistry(profiles, aliases)
    fable = reg.get("claude-fable-model-turn")
    assert fable.model == "claude-fable-5"
    assert fable.mode == "model_turn"
    assert {"tool_calling", "structured_events"} <= set(fable.capabilities)
    assert {"coordinator", "review"} <= set(fable.tags)
    assert fable.fallbacks == ["claude-model-turn"]
    assert reg.get("coordinator").id == "claude-fable-model-turn"

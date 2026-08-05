"""Semantic model/transport profile registry with live-readiness discovery.

Implements the ``scillm.transport_profile.v1`` contract (issue #27).

A profile describes how SciLLM can carry a Tau-controlled model turn or
provider session: provider/model selection, auth source, transport mode,
normalized transport capabilities, selection tags, limits, and ordered
fallbacks. Profiles never claim Tau-owned harness responsibilities (agent
tool loop, tool execution, evidence acceptance, node/DAG completion) —
declaring such a capability fails validation closed.

Readiness distinguishes: configured → credential_ready → transport_live_ready,
plus degraded (with explicit reason) and unavailable. ``transport_live_ready``
is only reported from a live probe readback, never from config presence.
"""

from __future__ import annotations

import os
import time
from typing import Any, Awaitable, Callable

import yaml
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from loguru import logger
from pydantic import BaseModel, Field, field_validator, model_validator

from scillm.proxy.errors import ProxyError

PROFILE_SCHEMA = "scillm.transport_profile.v1"
READINESS_SCHEMA = "scillm.transport_readiness.v1"

TRANSPORT_MODES = {"model_turn", "session_turn", "opaque_agent_compat"}

# Normalized transport capabilities a profile may advertise.
TRANSPORT_CAPABILITIES = {
    "streaming",
    "tool_calling",
    "structured_output",
    "files",
    "vision",
    "cancellation",
    "session_resume",
    "structured_events",
    "reasoning_effort",
}

# Harness responsibilities owned by Tau. A profile that advertises any of
# these is invalid — SciLLM is a transport, not an agent harness.
TAU_OWNED_CAPABILITIES = {
    "agent_loop",
    "tool_execution",
    "tool_authorization",
    "worktree_policy",
    "semantic_retry",
    "evidence_acceptance",
    "node_completion",
    "dag_completion",
}

SEMANTIC_TAGS = {
    "coordinator",
    "backend",
    "frontend",
    "documentation",
    "testing",
    "review",
    "vision",
    "local",
    "bulk",
}

READINESS_STATES = (
    "configured",
    "credential_ready",
    "transport_live_ready",
    "degraded",
    "unavailable",
)


class ProfileLimits(BaseModel):
    max_timeout_sec: int = 300
    max_turns_per_session: int = 64


class TransportProfile(BaseModel):
    schema_version: str = Field(default=PROFILE_SCHEMA, alias="schema")
    id: str
    label: str
    provider: str
    model: str
    auth_source: str
    mode: str
    reasoning_effort: str | None = None
    capabilities: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    limits: ProfileLimits = Field(default_factory=ProfileLimits)
    fallbacks: list[str] = Field(default_factory=list)
    caller_policy: dict[str, Any] = Field(default_factory=dict)

    model_config = {"populate_by_name": True}

    @field_validator("schema_version")
    @classmethod
    def _schema_pinned(cls, v: str) -> str:
        if v != PROFILE_SCHEMA:
            raise ValueError(f"unsupported profile schema {v!r}; expected {PROFILE_SCHEMA}")
        return v

    @field_validator("mode")
    @classmethod
    def _mode_known(cls, v: str) -> str:
        if v not in TRANSPORT_MODES:
            raise ValueError(f"unknown transport mode {v!r}; expected one of {sorted(TRANSPORT_MODES)}")
        return v

    @field_validator("capabilities")
    @classmethod
    def _capabilities_known(cls, caps: list[str]) -> list[str]:
        for cap in caps:
            if cap in TAU_OWNED_CAPABILITIES:
                raise ValueError(
                    f"capability {cap!r} is Tau-owned harness responsibility; "
                    "transport profiles must not advertise it"
                )
            if cap not in TRANSPORT_CAPABILITIES:
                raise ValueError(f"unknown transport capability {cap!r}")
        return caps

    @field_validator("tags")
    @classmethod
    def _tags_known(cls, tags: list[str]) -> list[str]:
        for tag in tags:
            if tag not in SEMANTIC_TAGS:
                raise ValueError(f"unknown semantic tag {tag!r}")
        return tags

    @model_validator(mode="after")
    def _mode_capability_contract(self) -> "TransportProfile":
        if self.mode == "opaque_agent_compat":
            # Compat transports must be honest about reduced control: they can
            # never claim the full Tau-native turn contract.
            forbidden = {"tool_calling", "structured_output"} & set(self.capabilities)
            if forbidden:
                raise ValueError(
                    "opaque_agent_compat profiles cannot advertise Tau-native turn "
                    f"capabilities {sorted(forbidden)}; the wrapped agent, not Tau, "
                    "controls its tool loop"
                )
        return self


class ProfileRegistry:
    def __init__(self, profiles: list[TransportProfile], aliases: dict[str, str]):
        self.profiles: dict[str, TransportProfile] = {}
        for p in profiles:
            if p.id in self.profiles:
                raise ValueError(f"duplicate profile id {p.id!r}")
            self.profiles[p.id] = p
        for p in profiles:
            for fb in p.fallbacks:
                if fb not in self.profiles:
                    raise ValueError(f"profile {p.id!r} declares unknown fallback {fb!r}")
        self.aliases: dict[str, str] = {}
        for role, pid in aliases.items():
            if pid not in self.profiles:
                raise ValueError(f"alias {role!r} resolves to unknown profile {pid!r}")
            self.aliases[role] = pid

    def get(self, profile_id: str) -> TransportProfile:
        pid = self.aliases.get(profile_id, profile_id)
        profile = self.profiles.get(pid)
        if profile is None:
            raise ProxyError(
                404,
                f"unknown transport profile {profile_id!r}",
                "unknown_transport_profile",
                details={"known_profiles": sorted(self.profiles), "aliases": self.aliases},
            )
        return profile

    def resolve(self, profile_id: str, required_capabilities: list[str]) -> TransportProfile:
        """Resolve a profile honoring required capabilities and declared fallbacks.

        Fails closed: unknown capability names are rejected; a fallback chain
        never silently downgrades capabilities — a candidate missing a required
        capability is skipped with the reason recorded, and if no candidate in
        the declared ordered chain satisfies the requirements the resolution
        errors instead of picking a weaker transport.
        """
        for cap in required_capabilities:
            if cap not in TRANSPORT_CAPABILITIES:
                raise ProxyError(
                    422,
                    f"unknown required capability {cap!r}",
                    "unknown_transport_capability",
                    details={"known_capabilities": sorted(TRANSPORT_CAPABILITIES)},
                )
        head = self.get(profile_id)
        skipped: list[dict[str, Any]] = []
        for candidate_id in [head.id, *head.fallbacks]:
            candidate = self.profiles.get(candidate_id)
            if candidate is None:  # pragma: no cover - registry construction forbids this
                raise ProxyError(500, f"fallback {candidate_id!r} missing from registry", "registry_corrupt")
            missing = [c for c in required_capabilities if c not in candidate.capabilities]
            if missing:
                skipped.append({"profile": candidate_id, "missing_capabilities": missing})
                continue
            return candidate
        raise ProxyError(
            422,
            f"no profile in the declared fallback chain of {profile_id!r} satisfies "
            f"required capabilities {required_capabilities}",
            "transport_capability_unsatisfied",
            details={"skipped": skipped},
        )


def _oauth_available(provider: str) -> bool:
    from scillm.proxy.providers.auth import is_anthropic_available, is_codex_available

    if provider == "anthropic-oauth":
        return is_anthropic_available()
    if provider == "codex-oauth":
        return is_codex_available()
    return False


def build_default_profiles(config: Any) -> tuple[list[TransportProfile], dict[str, str]]:
    """Derive the default registry from proxy configuration + OAuth mounts."""
    profiles: list[TransportProfile] = []

    def add(**kw: Any) -> None:
        profiles.append(TransportProfile(**kw))

    add(
        id="claude-fable-model-turn",
        label="Claude Fable 5 via Anthropic OAuth (premium coordinator model turn)",
        provider="anthropic-oauth",
        model="claude-fable-5",
        auth_source="~/.claude/.credentials.json",
        mode="model_turn",
        capabilities=[
            "streaming", "tool_calling", "structured_output", "vision",
            "cancellation", "structured_events",
        ],
        tags=["coordinator", "review"],
        fallbacks=["claude-model-turn"],
    )
    add(
        id="claude-model-turn",
        label="Claude Sonnet via Anthropic OAuth (Tau-native model turn)",
        provider="anthropic-oauth",
        model="claude-sonnet-4-6",
        auth_source="~/.claude/.credentials.json",
        mode="model_turn",
        capabilities=[
            "streaming", "tool_calling", "structured_output", "vision",
            "cancellation", "structured_events",
        ],
        tags=["coordinator", "backend", "review"],
        fallbacks=[],
    )
    add(
        id="codex-model-turn",
        label="GPT-5.5 Codex via ChatGPT OAuth (Tau-native model turn)",
        provider="codex-oauth",
        model="gpt-5.5",
        auth_source="~/.codex/auth.json",
        mode="model_turn",
        reasoning_effort="medium",
        capabilities=[
            "streaming", "tool_calling", "cancellation", "structured_events",
            "reasoning_effort",
        ],
        tags=["backend", "testing", "review"],
        fallbacks=["claude-model-turn"],
    )
    groups = set(getattr(config, "model_groups", {}) or {})
    if getattr(config, "chutes_api_base", None) and "text" in groups:
        add(
            id="chutes-text",
            label="Chutes text group (bulk model turns)",
            provider="chutes",
            model="text",
            auth_source="CHUTES_API_KEY",
            mode="model_turn",
            capabilities=["streaming", "structured_output", "cancellation", "structured_events"],
            tags=["bulk", "documentation"],
            fallbacks=[],
        )
    if getattr(config, "gemini_api_base", None):
        add(
            id="gemini-vlm",
            label="Gemini vision group",
            provider="gemini",
            model="vlm" if "vlm" in groups else "gemini-2.5-flash",
            auth_source="GEMINI_API_KEY_FREE/GEMINI_API_KEY_PAID",
            mode="model_turn",
            capabilities=[
                "streaming", "structured_output", "files", "vision",
                "cancellation", "structured_events",
            ],
            tags=["vision", "frontend"],
            fallbacks=["claude-model-turn"],
        )
    if getattr(config, "ollama_api_base", None):
        add(
            id="local-text",
            label="Local Ollama smoke model",
            provider="ollama",
            model="local-text",
            auth_source="none (local)",
            mode="model_turn",
            capabilities=["streaming", "cancellation", "structured_events"],
            tags=["local", "testing"],
            fallbacks=[],
        )
    add(
        id="opencode-serve-compat",
        label="OpenCode serve coding delegate (opaque agent, wrapped by Tau)",
        provider="opencode-serve",
        model="opencode",
        auth_source="OPENCODE_GO_API_KEY / provider OAuth",
        mode="opaque_agent_compat",
        capabilities=["streaming", "session_resume", "structured_events", "cancellation"],
        tags=["backend"],
        fallbacks=[],
    )

    have = {p.id for p in profiles}
    aliases_wanted = {
        "coordinator": "claude-fable-model-turn",
        "backend": "codex-model-turn",
        "frontend": "gemini-vlm",
        "documentation": "chutes-text",
        "testing": "local-text",
        "independent-review": "codex-model-turn",
    }
    fallback_alias = "claude-model-turn"
    aliases = {role: (pid if pid in have else fallback_alias) for role, pid in aliases_wanted.items()}
    return profiles, aliases


def load_registry(config: Any, overlay_path: str | None = None) -> ProfileRegistry:
    """Build the registry: defaults from config, then optional YAML overlay.

    Overlay file (default ``local/transport_profiles.yaml``) may add or replace
    profiles (``profiles:`` list) and aliases (``aliases:`` map). An invalid
    overlay fails closed — the proxy refuses to serve a registry it cannot
    validate rather than silently dropping entries.
    """
    profiles, aliases = build_default_profiles(config)
    path = overlay_path or os.environ.get(
        "SCILLM_TRANSPORT_PROFILES", "local/transport_profiles.yaml"
    )
    if path and os.path.exists(path):
        with open(path) as fh:
            overlay = yaml.safe_load(fh) or {}
        by_id = {p.id: p for p in profiles}
        for raw in overlay.get("profiles", []) or []:
            profile = TransportProfile(**raw)
            by_id[profile.id] = profile
        profiles = list(by_id.values())
        aliases.update(overlay.get("aliases", {}) or {})
    return ProfileRegistry(profiles, aliases)


# ---------------------------------------------------------------------------
# Readiness
# ---------------------------------------------------------------------------

LiveProbe = Callable[[TransportProfile], Awaitable[dict[str, Any]]]


def _credential_state(profile: TransportProfile) -> tuple[bool, str]:
    if profile.provider in ("anthropic-oauth", "codex-oauth"):
        ok = _oauth_available(profile.provider)
        return ok, ("oauth credentials mounted" if ok else "oauth credentials missing")
    if profile.provider == "chutes":
        ok = bool(os.environ.get("CHUTES_API_KEY"))
        return ok, ("CHUTES_API_KEY set" if ok else "CHUTES_API_KEY missing")
    if profile.provider == "gemini":
        ok = bool(os.environ.get("GEMINI_API_KEY_FREE") or os.environ.get("GEMINI_API_KEY_PAID") or os.environ.get("GEMINI_API_KEY"))
        return ok, ("gemini api key set" if ok else "gemini api key missing")
    if profile.provider == "ollama":
        return True, "local provider requires no credential"
    if profile.provider == "opencode-serve":
        ok = bool(os.environ.get("OPENCODE_GO_API_KEY")) or _oauth_available("codex-oauth") or _oauth_available("anthropic-oauth")
        return ok, ("delegate auth available" if ok else "no delegate auth available")
    return False, f"unknown provider {profile.provider!r}"


async def _known_model_ids() -> list[str]:
    """The proxy's live model listing (configured groups + auto-providers)."""
    import httpx

    from scillm.proxy import app as app_module

    master_key = app_module._config.general.master_key if app_module._config else ""
    transport = httpx.ASGITransport(app=app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://scillm.local", timeout=15) as client:
        resp = await client.get("/v1/models", headers={"Authorization": f"Bearer {master_key}"})
    if resp.status_code != 200:
        raise RuntimeError(f"/v1/models returned HTTP {resp.status_code}")
    return [str(m.get("id") or "") for m in resp.json().get("data", [])]


async def check_profile_model_known(
    profile: TransportProfile,
    known_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Validate the profile's model against the LIVE model listing.

    Registered profiles can outlive model renames/removals; a DAG that names a
    dead model should learn it at discovery time, not deep inside a node run.
    Wildcard-routed ids (``ollama:*``, ``chutes:Org/Model``) pass by prefix.
    """
    import difflib

    try:
        ids = known_ids if known_ids is not None else await _known_model_ids()
    except Exception as exc:  # noqa: BLE001 - discovery must degrade, not raise
        return {"known": True, "checked": False, "reason": f"model listing unavailable: {exc}"}
    if profile.model in ids:
        return {"known": True, "checked": True}
    if ":" in profile.model and any(i.split(":", 1)[0] == profile.model.split(":", 1)[0] and i.endswith("*") for i in ids):
        return {"known": True, "checked": True, "via": "wildcard"}
    return {
        "known": False,
        "checked": True,
        "suggestions": difflib.get_close_matches(profile.model, ids, n=5, cutoff=0.4),
    }


async def readiness_record(
    profile: TransportProfile,
    live_probe: LiveProbe | None = None,
) -> dict[str, Any]:
    """Compute one ``scillm.transport_readiness.v1`` record.

    Without a live probe the best state reachable is ``credential_ready`` —
    configuration presence alone is never reported as live readiness.
    """
    record: dict[str, Any] = {
        "schema": READINESS_SCHEMA,
        "profile": profile.id,
        "mode": profile.mode,
        "provider": profile.provider,
        "model": profile.model,
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "state": "configured",
        "evidence": {"configured": True},
    }
    cred_ok, cred_reason = _credential_state(profile)
    record["evidence"]["credential"] = cred_reason
    if not cred_ok:
        record["state"] = "unavailable"
        record["reason"] = cred_reason
        return record
    record["state"] = "credential_ready"
    if profile.mode != "opaque_agent_compat":
        model_check = await check_profile_model_known(profile)
        record["evidence"]["model_check"] = model_check
        if model_check.get("checked") and not model_check.get("known"):
            record["state"] = "degraded"
            record["reason"] = (
                f"model {profile.model!r} is not in the live /v1/models listing; "
                f"did you mean: {model_check.get('suggestions')}"
            )
            return record
    if live_probe is None:
        record["evidence"]["live_probe"] = "not run; live readiness requires ?live=true"
        return record
    started = time.monotonic()
    try:
        probe = await live_probe(profile)
        record["evidence"]["live_probe"] = probe
        record["evidence"]["latency_sec"] = round(time.monotonic() - started, 3)
        record["state"] = "transport_live_ready"
    except Exception as exc:  # noqa: BLE001 - readiness must report, not raise
        record["state"] = "degraded"
        record["reason"] = f"live probe failed: {exc}"
        record["evidence"]["latency_sec"] = round(time.monotonic() - started, 3)
    return record


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

AuthCheck = Callable[[Request], str | None]

_registry: ProfileRegistry | None = None


def get_registry() -> ProfileRegistry:
    global _registry
    if _registry is None:
        from scillm.proxy import app as app_module

        _registry = load_registry(app_module._config)
    return _registry


def reset_registry() -> None:
    global _registry
    _registry = None


async def _default_live_probe(profile: TransportProfile) -> dict[str, Any]:
    """One minimal real completion through the local chat surface."""
    import httpx

    from scillm.proxy import app as app_module

    if profile.mode == "opaque_agent_compat":
        raise RuntimeError(
            "opaque_agent_compat readiness is owned by its native surface "
            "(/v1/scillm/opencode); no generic live probe"
        )
    master_key = app_module._config.general.master_key if app_module._config else ""
    transport = httpx.ASGITransport(app=app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://scillm.local", timeout=90) as client:
        resp = await client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {master_key}",
                "X-Caller-Skill": "transport-profiles-readiness",
            },
            json={
                "model": profile.model,
                "messages": [{"role": "user", "content": "Reply with the single word: ready"}],
            },
        )
    if resp.status_code != 200:
        raise RuntimeError(f"live probe HTTP {resp.status_code}: {resp.text[:300]}")
    payload = resp.json()
    content = (payload.get("choices") or [{}])[0].get("message", {}).get("content", "")
    return {
        "resolved_model": payload.get("model"),
        "upstream_id": payload.get("id"),
        "usage": payload.get("usage"),
        "content_head": str(content)[:80],
    }


def create_transport_profiles_router(check_auth: AuthCheck, live_probe: LiveProbe | None = None) -> APIRouter:
    router = APIRouter()
    probe = live_probe or _default_live_probe

    def auth(request: Request) -> None:
        err = check_auth(request)
        if err:
            raise ProxyError(401, err, "authentication_error")

    @router.get("/profiles")
    async def list_profiles(request: Request) -> JSONResponse:
        auth(request)
        reg = get_registry()
        return JSONResponse({
            "schema": PROFILE_SCHEMA,
            "profiles": [p.model_dump(by_alias=True) for p in reg.profiles.values()],
            "aliases": reg.aliases,
        })

    @router.get("/profiles/capabilities")
    async def profile_capabilities(request: Request, require: str = "") -> JSONResponse:
        auth(request)
        reg = get_registry()
        required = [c for c in require.split(",") if c]
        for cap in required:
            if cap not in TRANSPORT_CAPABILITIES:
                raise ProxyError(
                    422,
                    f"unknown required capability {cap!r}",
                    "unknown_transport_capability",
                    details={"known_capabilities": sorted(TRANSPORT_CAPABILITIES)},
                )
        rows = []
        for p in reg.profiles.values():
            missing = [c for c in required if c not in p.capabilities]
            rows.append({
                "profile": p.id,
                "mode": p.mode,
                "capabilities": p.capabilities,
                "satisfies": not missing,
                "missing": missing,
            })
        return JSONResponse({
            "schema": PROFILE_SCHEMA,
            "known_capabilities": sorted(TRANSPORT_CAPABILITIES),
            "tau_owned_capabilities": sorted(TAU_OWNED_CAPABILITIES),
            "required": required,
            "profiles": rows,
        })

    @router.get("/profiles/readiness")
    async def profile_readiness(request: Request, profile: str = "", live: bool = False) -> JSONResponse:
        auth(request)
        reg = get_registry()
        targets = [reg.get(profile)] if profile else list(reg.profiles.values())
        records = []
        for p in targets:
            records.append(await readiness_record(p, probe if live else None))
        return JSONResponse({"schema": READINESS_SCHEMA, "live": live, "readiness": records})

    logger.info("transport profile routes registered under /v1/scillm/profiles")
    return router

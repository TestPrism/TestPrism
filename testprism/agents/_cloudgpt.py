"""LLM endpoint resolution for testprism agents.

Three routing modes, in priority order:

  1. **Explicit kwargs/env** (e.g. `--agent-kwarg api_key=... base_url=...`):
     point the agent at any OpenAI- or Anthropic-compatible gateway (PPAPI,
     self-hosted LiteLLM, OneAPI, etc.). This is the zero-touch path: no
     Azure, no AAD, no proxy — just a key and a URL.

  2. **CloudGPT** (model name starts with `cloudgpt/` or `azure/`, AND no
     explicit api_key given): Microsoft-internal Azure OpenAI behind
     `https://cloudgpt-openai.azure-api.net`. AAD token acquired via AzCli
     per `_env` read (~1h lifetime).

  3. **Provider-default** (no prefix, no kwargs): legacy behaviour —
     `openai/` uses `OPENAI_API_KEY`, `anthropic/` uses `ANTHROPIC_API_KEY`,
     etc. Preserved for back-compat with existing runs.

The two CloudGPT URL shapes:
  - Azure-style `/openai/...`: SDKs composing their own Azure path (litellm
    `azure/` prefix, aider, openhands, goose, codex config.toml).
  - OpenAI-compat `/openai/v1/...`: for clients that only speak vanilla
    OpenAI (qwen-code, opencode, agentscript).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

CLOUDGPT_ENDPOINT = "https://cloudgpt-openai.azure-api.net"
CLOUDGPT_AZURE_ENDPOINT = f"{CLOUDGPT_ENDPOINT}/openai"
CLOUDGPT_V1_ENDPOINT = f"{CLOUDGPT_ENDPOINT}/openai/v1"
CLOUDGPT_DEFAULT_API_VERSION = "2025-04-01-preview"


def get_cloudgpt_api_version() -> str:
    return os.environ.get("CLOUDGPT_API_VERSION", CLOUDGPT_DEFAULT_API_VERSION)


def get_cloudgpt_token() -> str:
    """Return a fresh AAD bearer token for CloudGPT.

    Prefers AzCli (headless-friendly, what LHB uses). Falls back to the broker
    flow the underlying helper picks automatically on interactive machines.
    Set CLOUDGPT_SKIP_TOKEN_VALIDATION=1 to skip the /openai/ping validation
    round-trip — useful in restricted network environments.
    """
    from testprism.agents._cloudgpt_aoai import get_openai_token_provider

    skip_validation = os.environ.get("CLOUDGPT_SKIP_TOKEN_VALIDATION", "1") == "1"
    provider = get_openai_token_provider(
        use_azure_cli=True,
        skip_access_validation=skip_validation,
    )
    return provider()


def is_cloudgpt_model(model_name: str | None) -> bool:
    """True when the model name selects a CloudGPT deployment.

    Accepts both `azure/<deployment>` (litellm-native) and
    `cloudgpt/<deployment>` (our alias). Plain `<deployment>` is not enough —
    we require an explicit prefix so direct OpenAI / Anthropic routes still
    work side-by-side.
    """
    if not model_name:
        return False
    prefix = model_name.split("/", 1)[0].lower()
    return prefix in {"azure", "cloudgpt"}


def strip_cloudgpt_prefix(model_name: str) -> str:
    """Return the bare deployment name (no `azure/` / `cloudgpt/` prefix)."""
    if "/" not in model_name:
        return model_name
    prefix, rest = model_name.split("/", 1)
    if prefix.lower() in {"azure", "cloudgpt"}:
        return rest
    return model_name


@dataclass(frozen=True)
class ResolvedEndpoint:
    """The three things every agent needs to talk to any LLM gateway.

    `mode` is a hint for agents that have to pick one of two code paths (e.g.
    openhands choosing between `LLM_BASE_URL` for OpenAI-compat vs.
    Anthropic-native). Agents that only care about api_key+base_url can
    ignore it.
    """

    api_key: str
    base_url: str | None
    api_version: str | None
    # "openai" | "anthropic" | "cloudgpt-azure" | "cloudgpt-v1"
    # "openai" covers any OpenAI-compatible gateway (PPAPI, OneAPI,
    # self-hosted vLLM, etc.). "cloudgpt-azure" means the SDK composes its
    # own `/openai/deployments/...` URL. "cloudgpt-v1" means the
    # `/openai/v1/...` compat layer on CloudGPT.
    mode: str
    # Whether this is a `cloudgpt/` or `azure/` model name — agents that
    # write Azure-style config (codex config.toml, goose azure_openai) key
    # off this.
    is_cloudgpt: bool


def _first(*values: str | None) -> str | None:
    for v in values:
        if v:
            return v
    return None


def resolve_endpoint(
    model_name: str | None,
    kwargs: dict[str, Any] | None = None,
    *,
    prefer_anthropic_native: bool = False,
) -> ResolvedEndpoint:
    """Pick api_key/base_url/api_version from kwargs, env, or CloudGPT.

    Precedence:
      1. kwargs `api_key` + `base_url` (+ optional `api_version`)
      2. env `LLM_API_KEY` + `LLM_BASE_URL` (+ `LLM_API_VERSION`)
      3. env `OPENAI_API_KEY` + `OPENAI_BASE_URL` (classic OpenAI-compat)
      4. env `ANTHROPIC_API_KEY` + `ANTHROPIC_BASE_URL` (if prefer_anthropic_native)
      5. CloudGPT (if model_name has `azure/` or `cloudgpt/` prefix)

    `base_url` can be None for direct OpenAI / Anthropic with no override.
    """
    kwargs = kwargs or {}
    env = os.environ

    explicit_key = _first(kwargs.get("api_key"), env.get("LLM_API_KEY"))
    explicit_base = _first(kwargs.get("base_url"), env.get("LLM_BASE_URL"))
    explicit_version = _first(
        kwargs.get("api_version"),
        env.get("LLM_API_VERSION"),
    )

    if explicit_key and explicit_base:
        mode = "anthropic" if prefer_anthropic_native else "openai"
        return ResolvedEndpoint(
            api_key=explicit_key,
            base_url=explicit_base,
            api_version=explicit_version,
            mode=mode,
            is_cloudgpt=False,
        )

    # kwargs/env also works when only a key was given and the agent can use
    # its native (no base_url) default — PPAPI-style users typically pass
    # both, but a user who sets ANTHROPIC_API_KEY alone should still work.
    if prefer_anthropic_native:
        a_key = _first(explicit_key, env.get("ANTHROPIC_API_KEY"))
        a_base = _first(explicit_base, env.get("ANTHROPIC_BASE_URL"))
        if a_key:
            return ResolvedEndpoint(
                api_key=a_key,
                base_url=a_base,
                api_version=explicit_version,
                mode="anthropic",
                is_cloudgpt=False,
            )
    else:
        o_key = _first(explicit_key, env.get("OPENAI_API_KEY"))
        o_base = _first(explicit_base, env.get("OPENAI_BASE_URL"))
        if o_key:
            return ResolvedEndpoint(
                api_key=o_key,
                base_url=o_base,
                api_version=explicit_version,
                mode="openai",
                is_cloudgpt=False,
            )

    if is_cloudgpt_model(model_name):
        mode = "cloudgpt-v1" if prefer_anthropic_native is False and kwargs.get(
            "cloudgpt_v1"
        ) else "cloudgpt-azure"
        return ResolvedEndpoint(
            api_key=get_cloudgpt_token(),
            base_url=(
                CLOUDGPT_V1_ENDPOINT if mode == "cloudgpt-v1"
                else CLOUDGPT_AZURE_ENDPOINT
            ),
            api_version=get_cloudgpt_api_version(),
            mode=mode,
            is_cloudgpt=True,
        )

    raise ValueError(
        "Cannot resolve LLM endpoint. Provide one of:\n"
        "  1. --agent-kwarg api_key=... --agent-kwarg base_url=...\n"
        "  2. LLM_API_KEY / LLM_BASE_URL environment variables\n"
        "  3. OPENAI_API_KEY (or ANTHROPIC_API_KEY for claude-code)\n"
        "  4. --model cloudgpt/<deployment> (requires az login)"
    )

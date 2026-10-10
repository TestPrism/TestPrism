import re

DEFAULT_MINI_SWE_AGENT_VERSION = "2.4.6"
MINI_SWE_AGENT_RUNTIME_LAYER_REVISION = "py311-v1"


def resolve_mini_swe_agent_version(version: str | None) -> str:
    resolved = str(version or DEFAULT_MINI_SWE_AGENT_VERSION).strip()
    return resolved or DEFAULT_MINI_SWE_AGENT_VERSION


def sanitize_image_tag_component(value: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip(".-")
    return sanitized[:120] or "default"


def mini_swe_agent_runtime_key(version: str | None) -> str:
    version_spec = resolve_mini_swe_agent_version(version)
    return (
        f"pypi-{sanitize_image_tag_component(version_spec)}-"
        f"{MINI_SWE_AGENT_RUNTIME_LAYER_REVISION}"
    )


def mini_swe_agent_base_image(
    version: str | None,
    explicit_image: str | None = None,
) -> str:
    if explicit_image:
        return explicit_image
    return f"testprism-mini-swe-agent-base:{mini_swe_agent_runtime_key(version)}"

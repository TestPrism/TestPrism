import json
import os
from collections.abc import Mapping
from pathlib import Path

from testprism.agents._cloudgpt import (
    CLOUDGPT_AZURE_ENDPOINT,
    get_cloudgpt_api_version,
    get_cloudgpt_token,
    is_cloudgpt_model,
    strip_cloudgpt_prefix,
)
from testprism.agents.agent_name import AgentName
from testprism.agents.base_agent import SystemPromptMode
from testprism.agents.installed_agents.abstract_installed_agent import (
    AbstractInstalledAgent,
)
from testprism.terminal.docker_compose_manager import DockerComposeManager
from testprism.terminal.models import TerminalCommand


def _toml_literal(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value)
    raise TypeError(f"unsupported TOML value type: {type(value).__name__}")


def _render_toml_lines(
    lines: list[str],
    table_name: str,
    values: Mapping[str, object],
) -> None:
    scalar_items: list[tuple[str, object]] = []
    nested_items: list[tuple[str, Mapping[str, object]]] = []
    for key, value in values.items():
        if isinstance(value, Mapping):
            nested_items.append((str(key), value))
        else:
            scalar_items.append((str(key), value))

    if table_name and (scalar_items or not nested_items):
        lines.append(f"[{table_name}]")

    for key, value in scalar_items:
        lines.append(f"{key} = {_toml_literal(value)}")

    if scalar_items and nested_items:
        lines.append("")

    for index, (key, nested) in enumerate(nested_items):
        nested_table = f"{table_name}.{key}" if table_name else key
        _render_toml_lines(lines, nested_table, nested)
        if index != len(nested_items) - 1:
            lines.append("")


def _render_toml_document(values: Mapping[str, object]) -> str:
    lines: list[str] = []
    _render_toml_lines(lines, "", values)
    return "\n".join(lines)


class CodexAgent(AbstractInstalledAgent):
    native_system_prompt_modes = frozenset({SystemPromptMode.REPLACE})

    @staticmethod
    def name() -> str:
        return AgentName.CODEX.value

    def __init__(
        self,
        model_name: str,
        api_key: str | None = None,
        base_url: str | None = None,
        api_version: str | None = None,
        *args,
        **kwargs,
    ):
        self._raw_codex_config = kwargs.get("codex_config")
        super().__init__(*args, **kwargs)
        self._use_cloudgpt = is_cloudgpt_model(model_name) and not api_key
        self._use_custom_gateway = bool(api_key and base_url) and not is_cloudgpt_model(
            model_name
        )
        self._model_name = (
            strip_cloudgpt_prefix(model_name)
            if self._use_cloudgpt
            else model_name.split("/")[-1]
        )
        self._explicit_key = api_key
        self._explicit_base = base_url
        self._explicit_version = api_version
        self._version = kwargs.get("version", "latest")
        self._sandbox_mode = "danger-full-access"
        self._model_provider: str | None = None
        self._provider_uses_inline_auth = False
        self._config_toml_body = self._build_config_toml_body()
        self._auth_json_body = self._build_auth_json_body()

    @property
    def _env(self) -> dict[str, str]:
        if self._use_cloudgpt:
            # The setup template writes a config.toml referencing these env
            # vars. Token refreshes per _env read (~1h lifetime).
            return {
                "CLOUDGPT_API_KEY": get_cloudgpt_token(),
                "CLOUDGPT_API_VERSION": get_cloudgpt_api_version(),
                "CLOUDGPT_BASE_URL": CLOUDGPT_AZURE_ENDPOINT,
            }
        if self._use_custom_gateway:
            # Codex CLI's custom-provider mechanism: the setup template
            # points [model_providers.cloudgpt] at the user's base_url with
            # wire_api="responses" (Codex dropped "chat" in late 2025).
            env = {
                "CLOUDGPT_API_KEY": self._explicit_key,
                "CLOUDGPT_BASE_URL": self._explicit_base,
            }
            if self._explicit_version:
                env["CLOUDGPT_API_VERSION"] = self._explicit_version
            return env
        return {
            "OPENAI_API_KEY": os.environ["OPENAI_API_KEY"],
        }

    @property
    def _install_agent_script_path(self) -> Path:
        return self._get_templated_script_path("codex-setup.sh.j2")

    def _get_template_variables(self) -> dict[str, str]:
        variables = super()._get_template_variables()
        variables["config_toml_body"] = self._config_toml_body or ""
        variables["auth_json_body"] = self._auth_json_body or ""
        return variables

    def _run_agent_commands(self, instruction: str) -> list[TerminalCommand]:
        log_dir = self.CONTAINER_AGENT_LOGS_PATH
        extra_flags = ""
        if self._model_provider:
            extra_flags = f"--config model_provider={self._model_provider} "
        if self.system_prompt:
            self._validate_system_prompt_delivery()
            extra_flags += (
                "--config "
                f"'model_instructions_file=\"{self.CONTAINER_SYSTEM_PROMPT_PATH}\"' "
            )
        return [
            TerminalCommand(
                command=(
                    f"mkdir -p {log_dir} && "
                    "set -o pipefail && "
                    "codex exec "
                    f"--sandbox {self._sandbox_mode} "
                    "--skip-git-repo-check "
                    f"{extra_flags}"
                    f"--model {self._model_name} "
                    "-- "  # end of flags
                    f"- < {self.CONTAINER_INSTRUCTION_PATH}"
                    f" 2>&1 | tee {log_dir}/codex.log"
                ),
                min_timeout_sec=0.0,
                max_timeout_sec=float("inf"),
                block=True,
                append_enter=True,
            )
        ]

    def _available_env_names(self) -> set[str]:
        if self.profile_env:
            return set(self.profile_env.keys())
        return set(self._env.keys())

    def _resolved_env_value(self, env_name: object) -> str:
        name = str(env_name)
        environment = self.profile_env if self.profile_env else self._env
        if name not in environment:
            raise ValueError(
                f"Codex config references undefined environment variable {name!r}"
            )
        return environment[name]

    def _env_reference(self, env_name: object) -> str:
        name = str(env_name)
        if name not in self._available_env_names():
            raise ValueError(
                f"Codex config references undefined environment variable {name!r}"
            )
        return f"${{{name}}}"

    def _container_reachable_service_url(self, base_url: str) -> str:
        return DockerComposeManager.container_reachable_loopback_url(base_url)

    def _normalize_provider_config(
        self,
        provider_name: str,
        raw_provider: object,
    ) -> dict[str, object]:
        if not isinstance(raw_provider, Mapping):
            raise TypeError(
                "codex_config.model_providers entries must be mappings; "
                f"got {type(raw_provider).__name__} for {provider_name!r}"
            )

        provider: dict[str, object] = {}
        if "name" in raw_provider:
            provider["name"] = str(raw_provider["name"])
        else:
            provider["name"] = provider_name
        if "wire_api" in raw_provider:
            provider["wire_api"] = str(raw_provider["wire_api"])

        base_url = raw_provider.get("base_url")
        base_url_env = raw_provider.get("base_url_env")
        if base_url is not None and base_url_env is not None:
            raise ValueError(
                f"Codex provider {provider_name!r} cannot set both base_url and "
                "base_url_env"
            )
        if base_url is not None:
            provider["base_url"] = self._container_reachable_service_url(str(base_url))
        elif base_url_env is not None:
            provider["base_url"] = self._container_reachable_service_url(
                self._resolved_env_value(base_url_env)
            )

        token_env = raw_provider.get("experimental_bearer_token_env")
        env_key = raw_provider.get("env_key")
        if token_env is not None and env_key is not None:
            raise ValueError(
                f"Codex provider {provider_name!r} cannot set both env_key and "
                "experimental_bearer_token_env"
            )
        if env_key is not None:
            self._env_reference(env_key)
            provider["env_key"] = str(env_key)
        if token_env is not None:
            provider["experimental_bearer_token"] = self._env_reference(token_env)

        query_params = raw_provider.get("query_params")
        if query_params is not None:
            if not isinstance(query_params, Mapping):
                raise TypeError(
                    "codex_config.model_providers.*.query_params must be a mapping"
                )
            provider["query_params"] = {
                str(key): value for key, value in query_params.items()
            }

        return provider

    def _legacy_config_document(self) -> dict[str, object] | None:
        if self._use_cloudgpt:
            return {
                "model_provider": "cloudgpt",
                "model": self._model_name,
                "model_providers": {
                    "cloudgpt": {
                        "name": "CloudGPT (Azure OpenAI)",
                        "base_url": self._container_reachable_service_url(
                            self._resolved_env_value("CLOUDGPT_BASE_URL")
                        ),
                        "env_key": "CLOUDGPT_API_KEY",
                        "wire_api": "responses",
                        "query_params": {
                            "api-version": self._env_reference("CLOUDGPT_API_VERSION")
                        },
                    }
                },
            }
        if self._use_custom_gateway:
            return {
                "model_provider": "cloudgpt",
                "model": self._model_name,
                "model_providers": {
                    "cloudgpt": {
                        "name": "Custom OpenAI-compatible gateway",
                        "base_url": self._container_reachable_service_url(
                            self._resolved_env_value("CLOUDGPT_BASE_URL")
                        ),
                        "env_key": "CLOUDGPT_API_KEY",
                        "wire_api": "responses",
                    }
                },
            }
        return None

    def _config_document(self) -> dict[str, object] | None:
        raw_config = self._raw_codex_config
        if raw_config is None:
            return self._legacy_config_document()
        if not isinstance(raw_config, Mapping):
            raise TypeError(
                "codex_config must be a mapping; "
                f"got {type(raw_config).__name__}"
            )

        config = {
            str(key): value
            for key, value in raw_config.items()
            if key != "model_providers"
        }
        providers = raw_config.get("model_providers")
        if providers is not None:
            if not isinstance(providers, Mapping):
                raise TypeError("codex_config.model_providers must be a mapping")
            config["model_providers"] = {
                str(provider_name): self._normalize_provider_config(
                    str(provider_name),
                    provider_config,
                )
                for provider_name, provider_config in providers.items()
            }

        config["model"] = self._model_name
        return config

    def _build_config_toml_body(self) -> str | None:
        config = self._config_document()
        if config is None:
            self._model_provider = None
            self._provider_uses_inline_auth = False
            return None

        providers = config.get("model_providers")
        provider_name = config.get("model_provider")
        if provider_name is None and isinstance(providers, Mapping) and len(providers) == 1:
            provider_name = next(iter(providers))
            config["model_provider"] = provider_name
        self._model_provider = str(provider_name) if provider_name is not None else None

        sandbox_mode = config.get("sandbox_mode")
        if sandbox_mode is not None:
            self._sandbox_mode = str(sandbox_mode)

        selected_provider = None
        if self._model_provider and isinstance(providers, Mapping):
            selected_provider = providers.get(self._model_provider)
        if isinstance(selected_provider, Mapping):
            self._provider_uses_inline_auth = bool(
                selected_provider.get("env_key")
                or selected_provider.get("experimental_bearer_token")
            )
        else:
            self._provider_uses_inline_auth = False

        return _render_toml_document(config)

    def _build_auth_json_body(self) -> str | None:
        if self._provider_uses_inline_auth:
            return None
        if "OPENAI_API_KEY" not in self._available_env_names():
            return None
        return '{\n  "OPENAI_API_KEY": "${OPENAI_API_KEY}"\n}'

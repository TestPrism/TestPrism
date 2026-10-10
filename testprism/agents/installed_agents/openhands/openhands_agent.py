import json
import os
import shlex
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from testprism.agents._cloudgpt import (
    CLOUDGPT_ENDPOINT,
    get_cloudgpt_api_version,
    get_cloudgpt_token,
    is_cloudgpt_model,
)
from testprism.agents.agent_name import AgentName
from testprism.agents.base_agent import SystemPromptMode
from testprism.agents.installed_agents.abstract_installed_agent import (
    AbstractInstalledAgent,
)
from testprism.terminal.docker_compose_manager import DockerComposeManager
from testprism.terminal.models import TerminalCommand
from testprism.terminal.tmux_session import TmuxSession
from testprism.utils.logger import logger


class OpenHandsAgent(AbstractInstalledAgent):
    CONTAINER_SYSTEM_PROMPT_PATH = Path(
        "/installed-agent/openhands-system-prompt.j2"
    )
    native_system_prompt_modes = frozenset({SystemPromptMode.REPLACE})
    _DEFAULT_CONTAINER_HOST_GATEWAY = "172.17.0.1"
    _LOCAL_SIDECAR_HOSTS = {"localhost", "127.0.0.1", "::1", "172.17.0.1", "host.docker.internal"}

    @staticmethod
    def name() -> str:
        return AgentName.OPENHANDS.value

    def __init__(
        self,
        model_name: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        api_version: str | None = None,
        *args,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self._model_name = model_name
        self._explicit_key = api_key
        self._explicit_base = base_url
        self._explicit_version = api_version
        self._git_version = kwargs.get("git_version", None)
        self._logger = logger.getChild(__name__)
        self._config = self._load_config(kwargs.get("config"))

    @staticmethod
    def _load_config(config_path: str | None) -> dict | None:
        if config_path is None:
            return None
        path = Path(config_path)
        if not path.exists():
            raise FileNotFoundError(f"Config file not found: {config_path}")
        with open(path) as f:
            config = json.load(f)
        logger.info(f"Loaded openhands config from {config_path}")
        return config

    def _get_effective_model(self) -> str:
        """Resolve the model from CLI or environment with README-compatible fallback."""
        if self._model_name:
            self._logger.debug(f"Using model {self._model_name} from CLI argument")
            return self._model_name

        if self._config and self._config.get("model"):
            model = self._config["model"]
            self._logger.debug(f"Using model {model} from agent config file")
            return model

        if self._config and "env" in self._config:
            llm_model = self._config["env"].get("LLM_MODEL")
            if llm_model:
                self._logger.debug(
                    f"Using model {llm_model} from agent config env.LLM_MODEL"
                )
                return llm_model

        if "LLM_MODEL" in os.environ:
            model = os.environ["LLM_MODEL"]
            self._logger.debug(f"Using model {model} from environment variable LLM_MODEL")
            return model

        if "ANTHROPIC_MODEL" in os.environ:
            model = os.environ["ANTHROPIC_MODEL"]
            self._logger.debug(
                f"Using model {model} from environment variable ANTHROPIC_MODEL"
            )
            return model

        if "DEFAULT_MODEL" in os.environ:
            model = os.environ["DEFAULT_MODEL"]
            self._logger.debug(
                f"Using model {model} from environment variable DEFAULT_MODEL"
            )
            return model

        raise ValueError(
            "No LLM model found. Please set one of: "
            "CLI --model, top-level 'model' or env.LLM_MODEL in --agent-config JSON, "
            "or LLM_MODEL / ANTHROPIC_MODEL / DEFAULT_MODEL in the environment"
        )

    def _resolve_api_key(self, effective_model: str) -> str:
        """Resolve API key with provider-aware fallbacks."""
        if "LLM_API_KEY" in os.environ:
            return os.environ["LLM_API_KEY"]

        model_prefix = effective_model.split("/", 1)[0].lower()

        # README-compatible behavior:
        # - anthropic/* prefers ANTHROPIC_API_KEY
        # - openai/* and azure/* prefer OPENAI_API_KEY (or Azure AD token)
        if model_prefix == "anthropic" and "ANTHROPIC_API_KEY" in os.environ:
            self._logger.info(
                "LLM_API_KEY not set, using ANTHROPIC_API_KEY for anthropic model"
            )
            return os.environ["ANTHROPIC_API_KEY"]

        if model_prefix in {"openai", "azure"}:
            if "OPENAI_API_KEY" in os.environ:
                self._logger.info(
                    "LLM_API_KEY not set, using OPENAI_API_KEY for openai/azure model"
                )
                return os.environ["OPENAI_API_KEY"]
            if "AZURE_OPENAI_AD_TOKEN" in os.environ:
                self._logger.info(
                    "LLM_API_KEY not set, using AZURE_OPENAI_AD_TOKEN for azure model"
                )
                return os.environ["AZURE_OPENAI_AD_TOKEN"]

        # Legacy fallback order for unspecified/other providers
        if "ANTHROPIC_API_KEY" in os.environ:
            self._logger.info(
                "LLM_API_KEY not set, using ANTHROPIC_API_KEY as fallback"
            )
            return os.environ["ANTHROPIC_API_KEY"]
        if "OPENAI_API_KEY" in os.environ:
            self._logger.info("LLM_API_KEY not set, using OPENAI_API_KEY as fallback")
            return os.environ["OPENAI_API_KEY"]

        raise ValueError(
            "No LLM API key found. Set LLM_API_KEY in the environment, under "
            "'env' in --agent-config JSON, or set ANTHROPIC_API_KEY / OPENAI_API_KEY / "
            "AZURE_OPENAI_AD_TOKEN (depending on model provider)."
        )

    @staticmethod
    def _user_supplied_llm_endpoint(env: dict[str, str]) -> bool:
        """True when the user already pointed LLM at a custom base (not managed CloudGPT).

        Used so `azure/<deployment>` can target a local SSH-tunnel sidecar or any
        private Azure-compatible proxy without tripping the AAD-backed CloudGPT path.
        """
        if env.get("LLM_BASE_URL"):
            return True
        return bool(
            os.environ.get("LLM_BASE_URL")
            or os.environ.get("OPENAI_BASE_URL")
            or os.environ.get("AZURE_API_BASE")
        )

    @classmethod
    def _normalize_container_llm_base_url(cls, base_url: str | None) -> str | None:
        """Rewrite localhost endpoints to host gateway for containerized OpenHands.

        Users often export ``AZURE_API_BASE=http://localhost:23437`` for host-side
        smoke checks. Inside the task container, ``localhost`` points to the
        container itself, so map loopback hosts to a host-reachable gateway.
        """
        if not base_url:
            return base_url

        gateway_host = os.environ.get(
            "OPENHANDS_CONTAINER_HOST_GATEWAY", cls._DEFAULT_CONTAINER_HOST_GATEWAY
        ).strip()
        if not gateway_host:
            return base_url

        parsed = urlparse(base_url)
        if not parsed.hostname:
            return base_url

        if parsed.hostname.lower() not in {"localhost", "127.0.0.1", "::1"}:
            return base_url

        host = gateway_host
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        netloc = host
        if parsed.port is not None:
            netloc = f"{host}:{parsed.port}"

        return urlunparse(
            (
                parsed.scheme,
                netloc,
                parsed.path,
                parsed.params,
                parsed.query,
                parsed.fragment,
            )
        )

    @classmethod
    def _is_local_azure_sidecar(cls, llm_model: str, base_url: str | None) -> bool:
        """Detect Azure-compatible sidecar endpoints reachable from local Docker."""
        if not llm_model.lower().startswith("azure/") or not base_url:
            return False
        parsed = urlparse(base_url)
        host = (parsed.hostname or "").lower()
        return host in cls._LOCAL_SIDECAR_HOSTS

    def _profile_container_env(self) -> dict[str, str]:
        """Complete an isolated profile environment without reading credentials.

        Patch-generation profiles deliberately bypass ``_env``: it may read a
        process-global key, which would let concurrently scheduled profiles
        leak credentials into one another.  OpenHands still needs its selected
        model and a few non-secret runtime switches, though.  Derive only those
        deterministic values here and leave all endpoint credentials supplied
        by the profile untouched.
        """

        env = dict(self.profile_env)
        env.setdefault("LLM_MODEL", self._model_name or "")
        if env["LLM_MODEL"].lower().startswith("cloudgpt/"):
            env["LLM_MODEL"] = "azure/" + env["LLM_MODEL"].split("/", 1)[1]

        env["AGENT_ENABLE_PROMPT_EXTENSIONS"] = "false"
        env["AGENT_ENABLE_BROWSING"] = "false"
        if self.system_prompt:
            env["AGENT_SYSTEM_PROMPT_FILENAME"] = str(
                self.CONTAINER_SYSTEM_PROMPT_PATH
            )
        env["ENABLE_BROWSER"] = "false"
        env["SANDBOX_ENABLE_AUTO_LINT"] = "true"
        env["SKIP_DEPENDENCY_CHECK"] = "1"
        env["SAVE_TRAJECTORY_PATH"] = DockerComposeManager.CONTAINER_AGENT_LOGS_PATH
        env["RUN_AS_OPENHANDS"] = "false"
        env["RUNTIME"] = "local"

        base_url = self._normalize_container_llm_base_url(env.get("LLM_BASE_URL"))
        if base_url:
            env["LLM_BASE_URL"] = base_url

        # The CloudGPT bridge is Azure-compatible and owns token refresh.  Use
        # the bearer-auth code path so LiteLLM does not choose an ``api-key``
        # header for the dummy value supplied by the sidecar configuration.
        if self._is_local_azure_sidecar(env["LLM_MODEL"], env.get("LLM_BASE_URL")):
            token = (
                env.get("AZURE_OPENAI_AD_TOKEN") or env.get("LLM_API_KEY") or ""
            ).strip()
            if token:
                env["AZURE_OPENAI_AD_TOKEN"] = token
                env.pop("LLM_API_KEY", None)

        return env

    def _create_env_setup_file(self) -> str:
        """Write a complete per-profile OpenHands environment.

        Other installed Agents can use the base implementation because their
        command line carries the model.  OpenHands reads it from ``LLM_MODEL``.
        """

        if not self.profile_env:
            return super()._create_env_setup_file()
        return "\n".join(
            f"export {key}={shlex.quote(value)}"
            for key, value in self._profile_container_env().items()
        )

    @property
    def _env(self) -> dict[str, str]:
        env: dict[str, str] = {}

        if self._config and "env" in self._config:
            env.update(self._config["env"])

        effective_model = self._get_effective_model()
        env.setdefault("LLM_MODEL", effective_model)
        llm_model = env["LLM_MODEL"]
        if llm_model.lower().startswith("cloudgpt/"):
            env["LLM_MODEL"] = "azure/" + llm_model.split("/", 1)[1]
            llm_model = env["LLM_MODEL"]

        use_managed_cloudgpt = is_cloudgpt_model(llm_model) and not (
            self._user_supplied_llm_endpoint(env)
        )

        if use_managed_cloudgpt:
            env.setdefault("LLM_API_KEY", get_cloudgpt_token())
            env.setdefault("LLM_BASE_URL", CLOUDGPT_ENDPOINT)
            env.setdefault("LLM_API_VERSION", get_cloudgpt_api_version())
        elif self._explicit_key and self._explicit_base:
            # Explicit gateway (PPAPI / OneAPI / self-hosted). Force the
            # litellm `openai/` prefix so it uses the OpenAI-compat code
            # path against the custom base_url.
            if "/" in env["LLM_MODEL"] and not env["LLM_MODEL"].lower().startswith(
                ("openai/", "azure/", "anthropic/")
            ):
                _, bare = env["LLM_MODEL"].split("/", 1)
                env["LLM_MODEL"] = f"openai/{bare}"
            elif "/" not in env["LLM_MODEL"]:
                env["LLM_MODEL"] = f"openai/{env['LLM_MODEL']}"
            env.setdefault("LLM_API_KEY", self._explicit_key)
            env.setdefault("LLM_BASE_URL", self._explicit_base)
            if self._explicit_version:
                env.setdefault("LLM_API_VERSION", self._explicit_version)
        else:
            if "LLM_API_KEY" not in env:
                env["LLM_API_KEY"] = self._resolve_api_key(llm_model)

            if "LLM_BASE_URL" not in env:
                if "LLM_BASE_URL" in os.environ:
                    env["LLM_BASE_URL"] = os.environ["LLM_BASE_URL"]
                elif "OPENAI_BASE_URL" in os.environ:
                    env["LLM_BASE_URL"] = os.environ["OPENAI_BASE_URL"]
                elif "AZURE_API_BASE" in os.environ:
                    env["LLM_BASE_URL"] = os.environ["AZURE_API_BASE"]

            if "LLM_API_VERSION" not in env:
                if "LLM_API_VERSION" in os.environ:
                    env["LLM_API_VERSION"] = os.environ["LLM_API_VERSION"]
                elif "AZURE_API_VERSION" in os.environ:
                    env["LLM_API_VERSION"] = os.environ["AZURE_API_VERSION"]

        # Disable browsing and prompt extensions since terminal-bench does not need them
        env["AGENT_ENABLE_PROMPT_EXTENSIONS"] = "false"
        env["AGENT_ENABLE_BROWSING"] = "false"
        env["ENABLE_BROWSER"] = "false"

        # Other sandbox settings
        env["SANDBOX_ENABLE_AUTO_LINT"] = "true"

        # Disable environment sanity check
        env["SKIP_DEPENDENCY_CHECK"] = "1"

        # Enable saving trajectory
        env["SAVE_TRAJECTORY_PATH"] = DockerComposeManager.CONTAINER_AGENT_LOGS_PATH

        # we are running in a sandboxed testing environment, no need to create
        # a new user
        env["RUN_AS_OPENHANDS"] = "false"

        # OpenHands runs inside task container, need to set the runtime to local
        env["RUNTIME"] = "local"

        # Finally, handle any other OpenHands environment variables
        for key, value in os.environ.items():
            if key.startswith("OPENHANDS_"):
                env[key.replace("OPENHANDS_", "")] = value
        if self.system_prompt:
            env["AGENT_SYSTEM_PROMPT_FILENAME"] = str(
                self.CONTAINER_SYSTEM_PROMPT_PATH
            )

        env["LLM_BASE_URL"] = self._normalize_container_llm_base_url(
            env.get("LLM_BASE_URL")
        ) or env.get("LLM_BASE_URL", "")
        if not env["LLM_BASE_URL"]:
            env.pop("LLM_BASE_URL")

        # For local Azure-compatible sidecars, LiteLLM should use bearer auth
        # (Authorization header). Passing LLM_API_KEY can trigger `api-key`
        # header, which some sidecars reject with 401.
        if self._is_local_azure_sidecar(env["LLM_MODEL"], env.get("LLM_BASE_URL")):
            token = (env.get("AZURE_OPENAI_AD_TOKEN") or env.get("LLM_API_KEY") or "").strip()
            if token:
                env["AZURE_OPENAI_AD_TOKEN"] = token
                env.pop("LLM_API_KEY", None)

        return env

    def _copy_system_prompt_to_container(self, session: TmuxSession) -> None:
        """Install a literal Jinja template consumed by OpenHands PromptManager."""

        if not self.system_prompt:
            return
        # OpenHands renders this file as Jinja. Put the complete prompt in a
        # quoted expression so prompt text containing ``{{ ... }}`` remains
        # literal rather than becoming executable template syntax.
        template = f"{{{{ {json.dumps(self.system_prompt.strip())} }}}}"
        self._copy_text_to_container(
            template,
            self.CONTAINER_SYSTEM_PROMPT_PATH,
            session,
        )

    def _get_template_variables(self) -> dict[str, str]:
        """
        Provide template variables for OpenHands setup script.

        Includes version and git_version for development builds.
        """
        variables = dict()

        if self._git_version:
            variables["git_version"] = self._git_version
        else:
            variables["version"] = self.version

        return variables

    @property
    def _install_agent_script_path(self) -> os.PathLike:
        return self._get_templated_script_path("openhands-setup.sh.j2")

    def _run_agent_commands(self, instruction: str) -> list[TerminalCommand]:
        log_dir = self.CONTAINER_AGENT_LOGS_PATH
        instruction_path = shlex.quote(str(self.CONTAINER_INSTRUCTION_PATH))

        # TestBench creates ``{task.workdir}/agent-tests`` before the agent runs. Resolve
        # that directory (or ``TASK_WORKDIR`` / ``TESTPRISM_TASK_WORKDIR`` from env) so
        # ``SANDBOX_VOLUMES`` matches ``testbench_harness._agent_test_dir`` without
        # changing the harness. Optional ``TASK_WORKDIR`` in ``--agent-config`` ``env``
        # overrides heuristics when auto-detection is insufficient.
        _resolve_task_root = (
            'if [ -n "${TESTPRISM_TASK_WORKDIR:-}" ] && '
            '[ -d "${TESTPRISM_TASK_WORKDIR:-}" ]; then '
            '_tw="$TESTPRISM_TASK_WORKDIR"; '
            'elif [ -n "${TASK_WORKDIR:-}" ] && [ -d "${TASK_WORKDIR:-}" ]; then '
            '_tw="$TASK_WORKDIR"; '
            'else _tw="$PWD"; '
            'for _c in "$PWD" /app /workspace /testbed /repo; do '
            '[ -d "$_c/agent-tests" ] && { _tw="$_c"; break; }; done; '
            'if [ "$_tw" = "$PWD" ]; then '
            '_f=$(find "$PWD" -maxdepth 14 -type d -name agent-tests 2>/dev/null | '
            'head -1); '
            '[ -n "$_f" ] && _tw=$(dirname "$_f"); fi; fi'
        )
        _py_has_core = (
            "/opt/openhands-venv/bin/python -c "
            + shlex.quote(
                "import importlib.util, sys; "
                "sys.exit(0 if importlib.util.find_spec('openhands.core.main') else 1)"
            )
        )
        _py_has_cli = (
            "/opt/openhands-venv/bin/python -c "
            + shlex.quote(
                "import importlib.util, sys; "
                "sys.exit(0 if importlib.util.find_spec('openhands.cli.main') else 1)"
            )
        )
        _llm_preflight_script = "\n".join(
            [
                "import os",
                "import socket",
                "import sys",
                "import urllib.parse",
                "",
                "u = os.environ.get('LLM_BASE_URL', '').strip()",
                "ok = False",
                "err = ''",
                "try:",
                "    p = urllib.parse.urlparse(u)",
                "    host = p.hostname or ''",
                "    port = p.port or (443 if p.scheme == 'https' else 80)",
                "    if host:",
                "        socket.create_connection((host, port), timeout=2.5).close()",
                "        ok = True",
                "    else:",
                "        err = 'LLM_BASE_URL is empty or invalid'",
                "except Exception as e:",
                "    err = str(e)",
                "if not ok:",
                "    print(",
                "        f'[OpenHands preflight] LLM endpoint unreachable: {u} ({err})',",
                "        file=sys.stderr,",
                "    )",
                "    print(",
                "        '[OpenHands preflight] Tip: ensure sidecar is reachable from task container (e.g. 172.17.0.1:23437).',",
                "        file=sys.stderr,",
                "    )",
                "    sys.exit(86)",
            ]
        )
        _llm_preflight = "/usr/bin/python3 -c " + shlex.quote(_llm_preflight_script)
        inner = (
            "{ "
            + _resolve_task_root
            + '; cd "$_tw" 2>/dev/null || cd "$PWD" || true; '
            + f'_task="$(cat {instruction_path})"; '
            + (
                'export SANDBOX_VOLUMES="${_tw}:/workspace:rw,'
                '${_tw}/agent-tests:${_tw}/agent-tests:rw,'
                '/tmp/testprism-agent-output:/tmp/testprism-agent-output:rw,'
                '/tmp/testprism-review:/tmp/testprism-review:ro"; '
            )
            + "if [ -n \"${LLM_BASE_URL:-}\" ]; then "
            + _llm_preflight
            + " || exit $?; fi; "
            + "if " + _py_has_core + "; then "
            + "/opt/openhands-venv/bin/python -m openhands.core.main "
            + '--task "$_task"; '
            + "elif " + _py_has_cli + "; then "
            + "/opt/openhands-venv/bin/python -m openhands.cli.main "
            + '--task "$_task"; '
            + "elif [ -x /opt/openhands-venv/bin/openhands ]; then "
            + '/opt/openhands-venv/bin/openhands --task "$_task"; '
            + "else "
            + "echo 'OpenHands: no openhands.core.main, openhands.cli.main, or openhands "
            + "executable in /opt/openhands-venv' >&2; exit 127; fi "
            + f"2>&1 | tee {log_dir}/openhands.log; }}"
        )
        command = f"mkdir -p {log_dir} && {inner}"

        return [
            TerminalCommand(
                command=command,
                min_timeout_sec=0.0,
                max_timeout_sec=float("inf"),
                block=True,
                append_enter=True,
            ),
        ]

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Iterable

from utils.app_paths import cache_dir
from utils.package_network import print_probe_report
from utils.package_network import probe_indexes
from utils.package_network import usable_indexes


def _configure_windows_console_output() -> None:
    """Prevent diagnostic Unicode from crashing legacy Windows consoles.

    Keep the console's chosen encoding (for example GBK/CP936) and only make
    unrepresentable characters replaceable. This avoids UnicodeEncodeError
    from status symbols such as check marks without forcing UTF-8 on users.
    """
    if platform.system() != "Windows":
        return
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(errors="replace")
        except (OSError, ValueError, TypeError):
            pass


_configure_windows_console_output()

# Server 源码所在目录（不随 PDF2ZH_HOME 变化），用于定位 server.py 与托管环境。
SERVER_ROOT = Path(__file__).resolve().parent.parent
# 模板保持原位：只作为「用户尚未配置 venv.json」时的默认来源。
DEFAULT_VENV_CONFIG = SERVER_ROOT / "config" / "venv.json.example"

ENGINE_ENV_NAMES = {
    "pdf2zh": "zotero-pdf2zh-venv",
    "pdf2zh_next": "zotero-pdf2zh-next-venv",
}
# 仅用于探测下载源是否可达（要求 PyPI 上存在同名发行版），不参与版本解析，
# 也不决定安装规格；pdf2zh 的安装源始终由 venv.json / 环境变量决定，因此不会
# 被这里的 PyPI 包名覆盖回上游。
PRIMARY_PACKAGES = {
    "pdf2zh": "pdf2zh",
    "pdf2zh_next": "pdf2zh-next",
}
# 读取已安装发行版的元数据。fork 与 PyPI 的发行版名都是 pdf2zh，所以
# read_versions / 健康探测对 git 安装同样有效（版本号来自 fork 的 pyproject）。
VERSION_PACKAGES = {
    "pdf2zh": ["pdf2zh", "BabelDOC", "PyMuPDF", "pypdf"],
    "pdf2zh_next": ["pdf2zh-next", "BabelDOC", "PyMuPDF", "pypdf"],
}
# 每个引擎对应的 CLI / 模块名，避免重复写 engine 二选一表达式。
CLI_MODULES = {
    "pdf2zh": "pdf2zh",
    "pdf2zh_next": "pdf2zh_next",
}
THINKING_FLAGS = (
    "--deepseek-thinking-mode",
    "--deepseek-reasoning-effort",
)
MIN_PDF2ZH_NEXT = (2, 9, 0)

# pdf2zh（PDFMathTranslate 1.x）从用户 fork 安装，因此托管依赖使用 PEP 508
# direct reference；pdf2zh_next 属于另一个上游项目，继续从 PyPI 安装。
# 直连 GitHub 时 pip/uv 还需要递归克隆 fork 的 git 子模块，实测容易卡住，
# 因此默认通过 gh-proxy 反向代理加速（主仓库与子模块都走代理）。
PDF2ZH_FORK_DIRECT_URL = "https://github.com/BluesilveEmperor/PDFtranslate.git"
PDF2ZH_PROXY_PREFIX = "https://gh-proxy.com/"
PDF2ZH_FORK_URL = PDF2ZH_PROXY_PREFIX + PDF2ZH_FORK_DIRECT_URL
PDF2ZH_FORK_REVISION = "main"
PDF2ZH_FORK_SPEC = f"pdf2zh @ git+{PDF2ZH_FORK_URL}@{PDF2ZH_FORK_REVISION}"
# 直连 fork 的等价规格，供用户无法使用代理时回退（通过环境变量指定）。
PDF2ZH_FORK_DIRECT_SPEC = (
    f"pdf2zh @ git+{PDF2ZH_FORK_DIRECT_URL}@{PDF2ZH_FORK_REVISION}"
)
# 子模块在 .gitmodules 中写的是 https://github.com/...，无法改文件，只能用
# git 的 url.<base>.insteadOf 在子进程环境里统一改写。
PDF2ZH_GITHUB_PREFIX = "https://github.com/"
PDF2ZH_GIT_PROXY_KEY = (
    f"url.{PDF2ZH_PROXY_PREFIX}{PDF2ZH_GITHUB_PREFIX}.insteadOf"
)
# 回退到 PyPI 上游时建议使用的版本（与 fork 的 pyproject.toml 保持一致）。
PDF2ZH_PYPI_FALLBACK_SPEC = "pdf2zh==1.9.12"
# 临时覆盖 pdf2zh 安装规格的环境变量。运行期 venv.json 的托管项会被模板自动
# 迁移，因此「长期回退到直连 / PyPI」需要通过该变量表达，而不是只改配置文件。
PDF2ZH_SPEC_ENV = "PDF2ZH_PDF2ZH_SPEC"

# 实测（gh-proxy，主仓库约 132 MB + 子模块约 65 MB）：首次 dry-run 预检约
# 795s；正式安装还要再次克隆并构建依赖。这里给出有限但足够的超时，不做无
# 上限等待，失败时仍会进入中文回退提示。
PDF2ZH_GIT_PREFLIGHT_TIMEOUT = 900
PDF2ZH_GIT_INSTALL_TIMEOUT = 1800


def managed_python_env(
    source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return an environment isolated from ambient Python package paths.

    Managed uv/conda runtimes must resolve packages from their own prefix. A
    machine-wide PYTHONPATH or PYTHONHOME can otherwise make a venv CLI import
    pure-Python files from the system installation while loading compiled
    extensions from somewhere else. Keep every unrelated variable (PATH,
    proxies, CUDA settings, credentials, and terminal settings) unchanged.
    """
    env = dict(os.environ if source is None else source)
    blocked = {"PYTHONHOME", "PYTHONPATH", "PYTHONNOUSERSITE"}
    for key in tuple(env):
        if key.upper() in blocked:
            env.pop(key, None)
    env["PYTHONNOUSERSITE"] = "1"
    return env


def requirement_package_name(requirement: str) -> str:
    """Return the normalized project name of a PEP 508 requirement.

    Works for both version pins (``pdf2zh==1.9.12``) and direct references
    (``pdf2zh @ git+https://...``), so managed-package bookkeeping keeps
    working when a package is installed from git.
    """
    raw = str(requirement).strip()
    name = re.split(r"[<>=!~\[\s;@]", raw, maxsplit=1)[0]
    return re.sub(r"[-_.]+", "-", name).lower()


def is_direct_reference(requirement: str) -> bool:
    """Return True for PEP 508 direct references such as ``pkg @ git+https://``."""
    raw = str(requirement)
    return "@" in raw and "://" in raw


# 只匹配「scheme://userinfo@」；userinfo 不含 /，不会误伤 URL 末尾的
# @main 等 revision 后缀。
_URL_CREDENTIALS_RE = re.compile(
    r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*://)(?P<userinfo>[^/@\s]+)@"
)


def _redact_requirement(text: str) -> str:
    """Hide credentials embedded in a requirement URL before printing it.

    ``https://user:token@host/repo.git`` becomes ``https://***@host/repo.git``.
    The scheme and host are kept so the printed source is still identifiable;
    ``@main`` revision suffixes are untouched because userinfo never contains
    ``/``.
    """
    return _URL_CREDENTIALS_RE.sub(r"\g<scheme>***@", str(text))


def is_pdf2zh_proxy_requirement(requirement: str) -> bool:
    """Return True when pdf2zh is installed through the gh-proxy mirror."""
    return (
        requirement_package_name(requirement) == "pdf2zh"
        and PDF2ZH_PROXY_PREFIX in str(requirement)
    )


def is_pdf2zh_fork_requirement(requirement: str) -> bool:
    """Return True when the requirement installs pdf2zh from the fork repository.

    Both the proxy URL and the direct GitHub URL count as the fork source; the
    helper is used for source reporting, while :func:`is_pdf2zh_proxy_requirement`
    decides whether the git proxy environment should be injected.
    """
    if requirement_package_name(requirement) != "pdf2zh":
        return False
    raw = str(requirement)
    return PDF2ZH_FORK_DIRECT_URL in raw or PDF2ZH_PROXY_PREFIX in raw


def _is_git_config_entry(key: str) -> bool:
    """Return True for ``GIT_CONFIG_KEY_n`` / ``GIT_CONFIG_VALUE_n`` variables."""
    return key.startswith("GIT_CONFIG_KEY_") or key.startswith(
        "GIT_CONFIG_VALUE_"
    )


def _pdf2zh_git_proxy_env(
    requirements: Iterable[str],
    source: Mapping[str, str] | None = None,
) -> dict[str, str] | None:
    """Return an env that rewrites github.com git URLs to the gh-proxy mirror.

    pip/uv always run ``git submodule update --init --recursive`` for a git
    requirement. The fork's ``.gitmodules`` points at a plain
    ``https://github.com/...`` URL, so installing through the proxy spec alone
    would still clone the submodule directly. ``url.<base>.insteadOf`` in the
    subprocess environment makes both the main repository and the submodule
    go through the proxy without touching the fork's files.

    Returns ``None`` (no injection) unless one of the requirements is the
    proxy-based pdf2zh fork spec, so direct/PyPI fallbacks stay untouched.

    Existing ``GIT_CONFIG_*`` settings always win: an unparsable or incomplete
    ``GIT_CONFIG_COUNT`` would make a rewrite drop the user's git configuration,
    so in that case we warn and skip the injection instead of touching it.
    """
    requirement_list = list(requirements)
    if not any(is_pdf2zh_proxy_requirement(item) for item in requirement_list):
        return None
    env = dict(os.environ if source is None else source)
    raw_count = str(env.get("GIT_CONFIG_COUNT") or "").strip()
    if not raw_count:
        if any(_is_git_config_entry(key) for key in env):
            print(
                "⚠️ 检测到未声明 GIT_CONFIG_COUNT 的 git 配置；"
                "为保留用户配置，跳过 gh-proxy 的 insteadOf 注入。"
            )
            return None
        count = 0
    else:
        try:
            count = int(raw_count)
        except ValueError:
            print(
                f"⚠️ GIT_CONFIG_COUNT={raw_count!r} 不是合法整数；"
                "为保留用户 git 配置，跳过 gh-proxy 的 insteadOf 注入。"
            )
            return None
        if count < 0 or any(
            f"GIT_CONFIG_KEY_{index}" not in env for index in range(count)
        ):
            print(
                "⚠️ 现有 GIT_CONFIG_* 条目与 GIT_CONFIG_COUNT 不一致；"
                "为保留用户 git 配置，跳过 gh-proxy 的 insteadOf 注入。"
            )
            return None
    # 用户/启动器可能已配置其他 GIT_CONFIG_*；已存在同名 insteadOf
    # 就不重复追加。
    for index in range(count):
        if env.get(f"GIT_CONFIG_KEY_{index}") == PDF2ZH_GIT_PROXY_KEY:
            return env
    env["GIT_CONFIG_COUNT"] = str(count + 1)
    env[f"GIT_CONFIG_KEY_{count}"] = PDF2ZH_GIT_PROXY_KEY
    env[f"GIT_CONFIG_VALUE_{count}"] = PDF2ZH_GITHUB_PREFIX
    return env


def _apply_pdf2zh_spec_override(packages: list[str]) -> list[str]:
    """Replace the managed pdf2zh requirement with ``PDF2ZH_PDF2ZH_SPEC``.

    Runtime ``venv.json`` managed entries are rewritten from the shipped
    template on every start, so editing the file alone cannot permanently pin
    pdf2zh back to a direct GitHub URL or PyPI. An environment variable is read
    here, after config loading, and therefore always wins over the migrated
    file content.
    """
    override = str(os.environ.get(PDF2ZH_SPEC_ENV) or "").strip()
    if not override:
        return packages
    if requirement_package_name(override) != "pdf2zh":
        print(
            f"⚠️ {PDF2ZH_SPEC_ENV} 不是 pdf2zh 依赖项，已忽略: "
            f"{_redact_requirement(override)}"
        )
        return packages
    print(
        f"ℹ️ 使用 {PDF2ZH_SPEC_ENV} 覆盖 pdf2zh 安装规格: "
        f"{_redact_requirement(override)}"
    )
    replaced = False
    result: list[str] = []
    for item in packages:
        if requirement_package_name(item) == "pdf2zh":
            if not replaced:
                result.append(override)
                replaced = True
            continue
        result.append(item)
    if not replaced:
        result.insert(0, override)
    return result


def _version_tuple(value: str | None) -> tuple[int, ...]:
    parts = re.findall(r"\d+", str(value or ""))
    return tuple(int(part) for part in parts[:4]) or (0,)


def pdf2zh_next_meets_minimum(version: str | None) -> bool:
    """Return True when the installed pdf2zh-next already satisfies 2.9.0."""
    if not version or str(version).strip().lower() in {"unknown", "none"}:
        return False
    return _version_tuple(version) >= MIN_PDF2ZH_NEXT


def environment_python_candidates(env_dir: Path) -> tuple[Path, ...]:
    """Return supported Python locations for a managed environment.

    On Windows, uv/venv puts Python in ``Scripts\\python.exe`` while Conda
    puts it at the environment root as ``<env>\\python.exe``.  The v4.1.0
    lifecycle originally assumed the uv layout for both managers, which made a
    healthy Windows Conda environment look broken immediately after creation.
    """
    env_dir = Path(env_dir)
    if platform.system() == "Windows":
        return (
            env_dir / "python.exe",
            env_dir / "python3.exe",
            env_dir / "Scripts" / "python.exe",
            env_dir / "Scripts" / "python3.exe",
        )
    return (env_dir / "bin" / "python", env_dir / "bin" / "python3")


def resolve_environment_python(env_dir: Path) -> Path:
    candidates = environment_python_candidates(env_dir)
    for candidate in candidates:
        if candidate.exists():
            return candidate
    # Callers still verify existence.  This fallback only gives diagnostics a
    # manager-appropriate expected path when environment creation is incomplete.
    env_dir = Path(env_dir)
    if platform.system() == "Windows" and (env_dir / "conda-meta").exists():
        return env_dir / "python.exe"
    return candidates[-1]


def resolve_environment_root(python_path: Path) -> Path:
    """Recover an environment root from either uv/venv or Conda Python."""
    python_path = Path(python_path)
    parent = python_path.parent
    if platform.system() == "Windows":
        return parent.parent if parent.name.lower() == "scripts" else parent
    return parent.parent if parent.name == "bin" else parent


def environment_path_entries(env_dir: Path, env_tool: str) -> list[Path]:
    """Return PATH entries needed to execute tools from a managed environment."""
    env_dir = Path(env_dir)
    if platform.system() != "Windows":
        candidates = [env_dir / "bin"]
    elif env_tool == "conda":
        # Important parts of a normal ``conda activate`` PATH on Windows.
        candidates = [
            env_dir,
            env_dir / "Library" / "mingw-w64" / "bin",
            env_dir / "Library" / "usr" / "bin",
            env_dir / "Library" / "bin",
            env_dir / "Scripts",
            env_dir / "bin",
        ]
    else:
        candidates = [env_dir / "Scripts"]

    result: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = os.path.normcase(os.path.abspath(str(candidate)))
        if candidate.exists() and key not in seen:
            seen.add(key)
            result.append(candidate)
    return result


def _python_in_env_dir(env_dir: Path) -> Path:
    # Compatibility alias for the lifecycle internals.
    return resolve_environment_python(env_dir)


def _bin_dir(env_dir: Path) -> Path:
    return env_dir / ("Scripts" if platform.system() == "Windows" else "bin")


def runtime_venv_config_path() -> Path:
    """运行期 venv.json 路径：``<程序根>/config/pdf-cache/venv.json``。

    旧版该文件位于 ``server/config``；模板 ``venv.json.example`` 仍留在
    ``server/config``，仅作为用户尚未生成配置时的默认来源。
    """
    return cache_dir() / "venv.json"


def load_environment_config(config_path: str | os.PathLike[str] | None = None) -> dict:
    path = Path(config_path) if config_path else runtime_venv_config_path()
    if not path.exists():
        # 用户尚未生成 venv.json（或旧版未迁移）时回退到随包模板
        path = DEFAULT_VENV_CONFIG
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def load_requirements(
    engine: str,
    env_tool: str,
    config_path: str | os.PathLike[str] | None = None,
) -> tuple[str, list[str]]:
    config = load_environment_config(config_path)
    engine_config = config.get(engine, {}).get(env_tool, {})
    packages = list(engine_config.get("packages", []))
    if engine == "pdf2zh":
        # 允许通过环境变量临时切换回 PyPI，且不会被配置迁移覆盖。
        packages = _apply_pdf2zh_spec_override(packages)
    return (
        str(engine_config.get("python_version", "3.12")),
        packages,
    )


def describe_pdf2zh_source(
    env_tool: str = "uv",
    config_path: str | os.PathLike[str] | None = None,
) -> str:
    """Describe where the managed pdf2zh requirement currently comes from.

    Used by ``manage_packages.py`` / ``update_packages.py`` so users do not
    mistake a fork install for "nothing was updated" when the PyPI version
    number did not change.
    """
    tool = env_tool if env_tool in {"uv", "conda"} else "uv"
    try:
        _, packages = load_requirements("pdf2zh", tool, config_path)
    except Exception as exc:
        return f"未知（读取 venv.json 失败: {exc}）"
    spec = next(
        (item for item in packages if requirement_package_name(item) == "pdf2zh"),
        None,
    )
    if spec is None:
        return "未声明 pdf2zh 依赖"
    if is_pdf2zh_proxy_requirement(spec):
        source = f"{PDF2ZH_FORK_URL}@{PDF2ZH_FORK_REVISION}"
        return f"fork 源（gh-proxy 代理）{_redact_requirement(source)}"
    if is_pdf2zh_fork_requirement(spec):
        source = f"{PDF2ZH_FORK_DIRECT_URL}@{PDF2ZH_FORK_REVISION}"
        return f"fork 源（直连）{_redact_requirement(source)}"
    if is_direct_reference(spec):
        return f"自定义 direct reference（{_redact_requirement(spec)}）"
    return f"PyPI（{_redact_requirement(spec)}）"


def _conda_info() -> dict | None:
    if not shutil.which("conda"):
        return None
    try:
        result = subprocess.run(
            ["conda", "info", "--json"],
            capture_output=True,
            text=True,
            check=True,
            env=managed_python_env(),
            timeout=60,
        )
        return json.loads(result.stdout)
    except Exception:
        return None


def find_conda_env_path(name: str) -> Path | None:
    info = _conda_info()
    if not info:
        return None
    for raw_path in info.get("envs", []):
        path = Path(raw_path)
        if path.name == name:
            return path
    for raw_dir in info.get("envs_dirs", []):
        path = Path(raw_dir) / name
        if path.exists():
            return path
    return None


def _query_conda_python(name: str) -> Path | None:
    """Ask conda which Python a named environment actually runs.

    Windows Conda layouts vary, and ``conda info --json`` can lag behind a
    freshly created environment. ``conda run -n ... python`` is the same
    lookup users get after ``conda activate``.
    """
    conda_path = shutil.which("conda")
    if not conda_path:
        return None
    command = [
        conda_path,
        "run",
        "-n",
        name,
        "python",
        "-c",
        "import sys; print(sys.executable)",
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            env=managed_python_env(),
            timeout=180,
        )
    except Exception:
        return None
    for line in reversed((result.stdout or "").splitlines()):
        candidate = Path(line.strip().strip('"'))
        if candidate.is_file():
            return candidate
    return None


def _resolve_named_conda_python(name: str, env_dir: Path | None = None) -> Path | None:
    env_dir = env_dir or find_conda_env_path(name)
    if env_dir:
        python_path = resolve_environment_python(env_dir)
        if python_path.exists():
            return python_path
    queried = _query_conda_python(name)
    if queried is not None and queried.exists():
        return queried
    return None


def find_existing_environment(
    engine: str,
    env_tool: str = "auto",
) -> tuple[str, Path, Path] | None:
    env_name = ENGINE_ENV_NAMES[engine]
    if env_tool in {"auto", "uv"}:
        env_dir = SERVER_ROOT / env_name
        python_path = _python_in_env_dir(env_dir)
        if python_path.exists():
            return "uv", env_dir, python_path
    if env_tool in {"auto", "conda"}:
        env_dir = find_conda_env_path(env_name)
        python_path = _resolve_named_conda_python(env_name, env_dir)
        if python_path is not None and python_path.exists():
            return "conda", resolve_environment_root(python_path), python_path
    return None


def read_versions(python_path: Path, engine: str) -> dict[str, str | None]:
    names = VERSION_PACKAGES[engine]
    code = (
        "import json; "
        "from importlib.metadata import version, PackageNotFoundError; "
        f"names={names!r}; out={{}}; "
        "\nfor name in names:\n"
        "    try: out[name]=version(name)\n"
        "    except PackageNotFoundError: out[name]=None\n"
        "print(json.dumps(out))"
    )
    result = subprocess.run(
        [str(python_path), "-I", "-c", code],
        capture_output=True,
        text=True,
        env=managed_python_env(),
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "无法读取翻译环境版本")
    return json.loads(result.stdout)


def format_versions(versions: dict[str, str | None]) -> str:
    return ", ".join(
        f"{name}={value}" for name, value in versions.items() if value is not None
    )


def _runtime_command(python_path: Path, module: str) -> list[str]:
    env_dir = resolve_environment_root(python_path)
    executable = _bin_dir(env_dir) / (
        module + (".exe" if platform.system() == "Windows" else "")
    )
    if executable.exists():
        return [str(executable)]
    return [str(python_path), "-m", module]


def runtime_supports_deepseek_thinking(
    python_path: Path,
    *,
    env: Mapping[str, str] | None = None,
    isolated: bool = False,
) -> bool:
    """Check DeepSeek V4 capability without importing or starting pdf2zh_next.

    pdf2zh_next 2.9.0 imports BabelDOC/high-level modules before its CLI parser
    runs, so ``pdf2zh_next --help`` is not a safe health probe: on some Macs it
    can spend more than a minute in import/asset initialization.  We instead
    inspect the installed distribution files using importlib.metadata from the
    target interpreter.  This verifies both DeepSeek setting fields and the
    generic underscore-to-hyphen CLI argument generation used upstream.
    """
    code = (
        "import json; "
        "from importlib.metadata import distribution; "
        "from pathlib import Path; "
        "out={'supported': False, 'reason': ''}; "
        "\ntry:\n"
        "    dist=distribution('pdf2zh-next')\n"
        "    settings=Path(dist.locate_file('pdf2zh_next/config/translate_engine_model.py'))\n"
        "    cli=Path(dist.locate_file('pdf2zh_next/config/main.py'))\n"
        "    settings_text=settings.read_text(encoding='utf-8')\n"
        "    cli_text=cli.read_text(encoding='utf-8')\n"
        "    fields=('deepseek_thinking_mode','deepseek_reasoning_effort')\n"
        "    field_ok=all(field in settings_text for field in fields)\n"
        "    cli_ok='field_name.replace(\"_\", \"-\").lower()' in cli_text\n"
        "    out={'supported': bool(field_ok and cli_ok), 'reason': 'ok' if field_ok and cli_ok else 'missing-fields-or-cli-mapping'}\n"
        "except Exception as exc:\n"
        "    out={'supported': False, 'reason': type(exc).__name__ + ': ' + str(exc)}\n"
        "print(json.dumps(out))"
    )
    try:
        runtime_env = (
            managed_python_env(env)
            if isolated
            else dict(os.environ if env is None else env)
        )
        command = [str(python_path)]
        if isolated:
            command.append("-I")
        command.extend(["-c", code])
        result = subprocess.run(
            command,
            cwd=SERVER_ROOT,
            capture_output=True,
            text=True,
            env=runtime_env,
            timeout=20,
        )
        if result.returncode != 0:
            return False
        payload = json.loads(result.stdout.strip() or "{}")
        return bool(payload.get("supported"))
    except Exception:
        return False


def _package_manager_env() -> dict[str, str]:
    env = managed_python_env()
    env.setdefault("UV_HTTP_CONNECT_TIMEOUT", "10")
    env.setdefault("UV_HTTP_TIMEOUT", "120")
    env.setdefault("UV_HTTP_RETRIES", "5")
    return env


def _install_subprocess_env(requirements: Iterable[str]) -> dict[str, str]:
    """Environment for pip/uv install subprocesses.

    pdf2zh 走 gh-proxy 代理源时，额外注入 git ``insteadOf``，让主仓库与
    ``.gitmodules`` 里的子模块都经过代理；回退到直连 / PyPI 时保持原样。
    """
    base = _package_manager_env()
    return _pdf2zh_git_proxy_env(requirements, base) or base


def _pip_supports_dry_run(python_path: Path) -> bool:
    try:
        result = subprocess.run(
            [str(python_path), "-I", "-m", "pip", "install", "--help"],
            capture_output=True,
            text=True,
            env=managed_python_env(),
            timeout=30,
        )
        return result.returncode == 0 and "--dry-run" in (result.stdout or "")
    except Exception:
        return False


def _build_install_command(
    env_tool: str,
    python_path: Path,
    requirements: Iterable[str],
    index_url: str,
    *,
    dry_run: bool,
    reinstall: bool = False,
) -> list[str] | None:
    requirements = list(requirements)
    if env_tool == "uv":
        uv_path = shutil.which("uv")
        if not uv_path:
            return None
        command = [uv_path, "pip", "install"]
        if dry_run:
            command.append("--dry-run")
        elif reinstall:
            command.append("--reinstall")
        command.extend(
            ["--index-url", index_url, *requirements, "--python", str(python_path)]
        )
        return command
    if dry_run and not _pip_supports_dry_run(python_path):
        return None
    command = [str(python_path), "-I", "-m", "pip", "install"]
    if dry_run:
        command.append("--dry-run")
    elif reinstall:
        command.append("--force-reinstall")
    command.extend(["--index-url", index_url, *requirements])
    return command


def _resolver_preflight(
    env_tool: str,
    python_path: Path,
    requirements: list[str],
    index_url: str,
) -> bool:
    command = _build_install_command(
        env_tool, python_path, requirements, index_url, dry_run=True
    )
    if command is None:
        return env_tool == "conda"
    # 含 git 等 direct reference 时解析器需要先克隆仓库（pdf2zh fork 还要递归
    # 克隆子模块，实测约 13 分钟），给足一次性预检超时；非 git 依赖保持原值。
    timeout = (
        PDF2ZH_GIT_PREFLIGHT_TIMEOUT
        if any(is_direct_reference(item) for item in requirements)
        else 180
    )
    try:
        result = subprocess.run(
            command,
            cwd=SERVER_ROOT,
            capture_output=True,
            text=True,
            env=_install_subprocess_env(requirements),
            timeout=timeout,
        )
        return result.returncode == 0
    except Exception:
        return False


def choose_install_sources(
    engine: str,
    env_tool: str,
    python_path: Path,
    requirements: list[str],
    preferred_index: str | None = None,
    network_timeout: float = 4.0,
) -> list[str]:
    results = probe_indexes(
        PRIMARY_PACKAGES[engine],
        preferred_index=preferred_index,
        timeout=network_timeout,
    )
    print_probe_report(results)
    sources: list[str] = []
    git_source = any(is_direct_reference(item) for item in requirements)
    # git 源每次预检都要重新克隆仓库（pip 不缓存 VCS 克隆），因此只保留前
    # 2 个通过预检的源：既能控制耗时，又能在首源正式安装失败时回退。
    max_sources = 2 if git_source else None
    for result in usable_indexes(results):
        if _resolver_preflight(env_tool, python_path, requirements, result.url):
            sources.append(result.url)
            if max_sources is not None and len(sources) >= max_sources:
                print(
                    f"ℹ️ git 源解析成本较高，已保留前 {max_sources} 个"
                    "通过预检的下载源用于回退。"
                )
                break
    return sources


def _remove_conda_env(name: str) -> None:
    if not shutil.which("conda") or not find_conda_env_path(name):
        return
    try:
        subprocess.run(
            ["conda", "env", "remove", "-n", name, "-y"],
            cwd=SERVER_ROOT,
            check=False,
            env=managed_python_env(),
            timeout=1200,
        )
    except Exception:
        pass


def _create_uv_environment(path: Path, python_version: str) -> Path:
    uv_path = shutil.which("uv")
    if not uv_path:
        raise RuntimeError("未找到 uv")
    existing = resolve_environment_python(path)
    if existing.exists():
        return existing
    shutil.rmtree(path, ignore_errors=True)
    subprocess.run(
        [uv_path, "venv", str(path), "--python", python_version],
        cwd=SERVER_ROOT,
        check=True,
        env=_package_manager_env(),
        timeout=1200,
    )
    python_path = resolve_environment_python(path)
    if not python_path.exists():
        raise RuntimeError(f"uv 环境没有生成 Python 可执行文件: {path}")
    return python_path


def _create_conda_environment(name: str, python_version: str) -> tuple[Path, Path]:
    conda_path = shutil.which("conda")
    if not conda_path:
        raise RuntimeError("未找到 conda")
    python_path = _resolve_named_conda_python(name)
    if python_path is not None:
        return resolve_environment_root(python_path), python_path
    subprocess.run(
        [
            conda_path,
            "create",
            "-n",
            name,
            f"python={python_version}",
            "pip",
            "-y",
        ],
        cwd=SERVER_ROOT,
        check=True,
        env=managed_python_env(),
        timeout=1200,
    )
    python_path = _resolve_named_conda_python(name)
    if python_path is None or not python_path.exists():
        raise RuntimeError(
            f"conda 环境 {name} 创建后仍找不到 Python 可执行文件。"
            "请在终端执行 `conda activate " + name + "` 后运行 `where python`（Windows）"
            "或 `which python` 确认环境是否可用。"
        )
    return resolve_environment_root(python_path), python_path


def _cleanup_legacy_sidecar_environments(engine: str, env_tool: str) -> None:
    env_name = ENGINE_ENV_NAMES[engine]
    if env_tool == "uv":
        shutil.rmtree(SERVER_ROOT / f"{env_name}.staging", ignore_errors=True)
        shutil.rmtree(SERVER_ROOT / f"{env_name}.backup", ignore_errors=True)
        return
    _remove_conda_env(f"{env_name}-staging")
    _remove_conda_env(f"{env_name}-backup")


def _ensure_canonical_environment(
    engine: str,
    env_tool: str,
    python_version: str,
) -> tuple[Path, Path]:
    env_name = ENGINE_ENV_NAMES[engine]
    if env_tool == "uv":
        env_dir = SERVER_ROOT / env_name
        python_path = _create_uv_environment(env_dir, python_version)
        return env_dir, python_path
    return _create_conda_environment(env_name, python_version)


def _report_pdf2zh_fork_install_hint(
    engine: str,
    requirements: Iterable[str] = (),
) -> None:
    """Explain how to recover when the fork (git) install fails.

    pdf2zh 现在通过 PEP 508 direct reference 从 fork 仓库安装，这比 PyPI 版本
    多出「本机可用 git + 能访问 GitHub」两项前提；默认还通过 gh-proxy 代理
    主仓库与子模块。失败时给出中文回退方案，避免用户误以为只能放弃更新。
    只在 fork 源（代理或直连）场景打印，回退到 PyPI 时不打印。
    """
    if engine != "pdf2zh":
        return
    requirement_list = list(requirements)
    fork_specs = [
        item for item in requirement_list if is_pdf2zh_fork_requirement(item)
    ]
    if requirement_list and not fork_specs:
        return
    uses_proxy = (
        any(is_pdf2zh_proxy_requirement(item) for item in fork_specs)
        if fork_specs
        else True
    )
    current_spec = fork_specs[0] if fork_specs else PDF2ZH_FORK_SPEC
    print(
        "💡 pdf2zh 当前从 fork 源安装: "
        f"{_redact_requirement(current_spec)}"
    )
    print(
        "💡 该方式需要本机可执行 git，并且能访问 GitHub；"
        "请确认 git 已安装、网络/代理可用后重试。"
    )
    if uses_proxy:
        print(
            f"💡 已通过 {PDF2ZH_PROXY_PREFIX} 代理克隆主仓库与 git 子模块；"
            "首次安装仍需下载较长时间。"
        )
    else:
        print("💡 当前直连 GitHub（未注入 gh-proxy）；首次安装会连同 git 子模块一起下载。")
    print("💡 也可回退（启动时的配置迁移不会覆盖环境变量）：")
    direct_fallback = _redact_requirement(PDF2ZH_FORK_DIRECT_SPEC)
    pypi_fallback = _redact_requirement(PDF2ZH_PYPI_FALLBACK_SPEC)
    if uses_proxy:
        print(
            f"   1) 临时设置环境变量 {PDF2ZH_SPEC_ENV}=\"{direct_fallback}\""
            " 改走直连（不注入代理）；"
        )
        print(
            f"   2) 或设置 {PDF2ZH_SPEC_ENV}={pypi_fallback} 改走 PyPI；"
        )
    else:
        print(
            f"   1) 临时设置环境变量 {PDF2ZH_SPEC_ENV}={pypi_fallback}"
            " 改走 PyPI；"
        )
    print(
        f"   {3 if uses_proxy else 2}) 也可把 venv.json 中该项改为上述版本"
        "（下次启动时托管项可能被模板迁移回代理源，长期回退请使用环境变量）。"
    )


def _run_install(
    env_tool: str,
    python_path: Path,
    requirements: list[str],
    sources: list[str],
    *,
    reinstall: bool = False,
) -> bool:
    for position, index_url in enumerate(sources, start=1):
        command = _build_install_command(
            env_tool,
            python_path,
            requirements,
            index_url,
            dry_run=False,
            reinstall=reinstall,
        )
        if command is None:
            return False
        print(f"\n📦 使用下载源: {index_url}")
        # git 源需要再次克隆仓库与子模块，正式安装超时比普通依赖更宽。
        timeout = (
            PDF2ZH_GIT_INSTALL_TIMEOUT
            if any(is_direct_reference(item) for item in requirements)
            else 1200
        )
        try:
            subprocess.run(
                command,
                cwd=SERVER_ROOT,
                check=True,
                env=_install_subprocess_env(requirements),
                timeout=timeout,
            )
            return True
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            print(f"❌ 安装失败: {exc}")
            if position < len(sources):
                print("↪ 自动切换到下一已验证下载源。")
    return False


def managed_runtime_health(
    engine: str,
    python_path: Path,
) -> tuple[bool, str]:
    """Quickly verify native dependencies resolve inside a managed runtime.

    Metadata-only checks miss damaged binary wheels. In particular, Pydantic 2
    can leave importable Python wrappers while ``_pydantic_core`` is absent or
    is accidentally resolved from a global Python installation. Avoid the
    heavyweight pdf2zh_next CLI startup and import only its small critical
    native dependency chain.
    """
    if engine != "pdf2zh_next":
        return True, "ok"

    code = (
        "import importlib, json, os, sys; "
        "root=os.path.normcase(os.path.realpath(sys.prefix)); "
        "names=('pydantic','pydantic_core','pydantic_core._pydantic_core'); "
        "out={'ok': True, 'reason': 'ok', 'root': root}; "
        "\ntry:\n"
        "    for name in names:\n"
        "        module=importlib.import_module(name)\n"
        "        origin=getattr(module, '__file__', None)\n"
        "        if not origin:\n"
        "            raise RuntimeError(name + ' 没有可验证的文件路径')\n"
        "        origin=os.path.normcase(os.path.realpath(origin))\n"
        "        try:\n"
        "            inside=os.path.commonpath((root, origin)) == root\n"
        "        except ValueError:\n"
        "            inside=False\n"
        "        if not inside:\n"
        "            raise RuntimeError(name + ' 来自托管环境之外: ' + origin)\n"
        "except Exception as exc:\n"
        "    out={'ok': False, 'reason': type(exc).__name__ + ': ' + str(exc), 'root': root}\n"
        "print(json.dumps(out, ensure_ascii=False))"
    )
    try:
        result = subprocess.run(
            [str(python_path), "-I", "-c", code],
            cwd=SERVER_ROOT,
            capture_output=True,
            text=True,
            env=managed_python_env(),
            timeout=30,
        )
        if result.returncode != 0:
            reason = (result.stderr or result.stdout or "unknown error").strip()
            return False, reason
        payload = json.loads(result.stdout.strip() or "{}")
        return bool(payload.get("ok")), str(payload.get("reason") or "unknown error")
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def validate_environment(
    engine: str,
    python_path: Path,
    requirements: list[str],
    *,
    require_deepseek_thinking: bool = False,
) -> bool:
    try:
        code = (
            "from packaging.requirements import Requirement; "
            "from importlib.metadata import version; "
            f"reqs={requirements!r}; "
            "\nfor raw in reqs:\n"
            "    req=Requirement(raw)\n"
            "    installed=version(req.name)\n"
            "    assert not req.specifier or req.specifier.contains(installed), (raw, installed)\n"
        )
        result = subprocess.run(
            [str(python_path), "-I", "-c", code],
            cwd=SERVER_ROOT,
            capture_output=True,
            text=True,
            env=managed_python_env(),
            timeout=90,
        )
        if result.returncode != 0:
            # packaging 的错误会回显 requirement 原文，打印前先脱敏 URL 凭据。
            detail = (result.stderr or result.stdout).strip()
            print("❌ 环境依赖完整性检查失败:", _redact_requirement(detail))
            return False
        healthy, reason = managed_runtime_health(engine, python_path)
        if not healthy:
            print(f"❌ {engine} 关键运行时依赖检查失败: {reason}")
            return False
        module = CLI_MODULES.get(engine, CLI_MODULES["pdf2zh"])
        env_dir = resolve_environment_root(python_path)
        suffix = ".exe" if platform.system() == "Windows" else ""
        cli_name = module + suffix
        executable = next(
            (
                candidate
                for candidate in (
                    _bin_dir(env_dir) / cli_name,
                    env_dir / cli_name,
                )
                if candidate.exists()
            ),
            None,
        )
        if executable is None:
            print(f"❌ {module} CLI 入口不存在: {_bin_dir(env_dir) / cli_name}")
            return False

        # Do not launch the heavyweight pdf2zh_next CLI help path here. Upstream imports
        # BabelDOC/high-level modules before CLI parsing, so --help can be a
        # heavyweight operation and is not a reliable installation probe.
        if require_deepseek_thinking and engine == "pdf2zh_next":
            if not runtime_supports_deepseek_thinking(
                python_path,
                env=managed_python_env(),
                isolated=True,
            ):
                print("❌ pdf2zh_next 缺少 DeepSeek V4 thinking capability")
                return False
        return True
    except Exception as exc:
        print(f"❌ 环境验证失败: {exc}")
        return False


def transactional_install_or_update(
    engine: str = "pdf2zh_next",
    *,
    env_tool: str = "auto",
    config_path: str | os.PathLike[str] | None = None,
    preferred_index: str | None = None,
    network_timeout: float = 4.0,
    require_deepseek_thinking: bool = False,
    force_reinstall: bool = False,
) -> tuple[bool, str | None, Path | None]:
    if engine not in ENGINE_ENV_NAMES:
        raise ValueError(f"Unknown engine: {engine}")
    existing = find_existing_environment(engine, env_tool)
    selected_tool = existing[0] if existing else None
    if selected_tool is None:
        candidates = [env_tool] if env_tool in {"uv", "conda"} else ["uv", "conda"]
        selected_tool = next((tool for tool in candidates if shutil.which(tool)), None)
    if selected_tool is None:
        print("❌ 未找到可用的 uv 或 conda，无法创建翻译环境。")
        return False, None, None
    python_version, requirements = load_requirements(engine, selected_tool, config_path)
    if not requirements:
        print(f"❌ {engine} 没有声明安装依赖。")
        return False, selected_tool, existing[1] if existing else None
    try:
        _cleanup_legacy_sidecar_environments(engine, selected_tool)
        print("\n📦 将在当前翻译环境中直接安装/更新依赖，不再创建 staging 或 backup 环境。")
        if existing:
            env_dir, python_path = existing[1], existing[2]
            print(f"   使用已有 {selected_tool} 环境: {env_dir}")
            print(f"   Python: {python_path}")
        else:
            print(f"   正在创建 {selected_tool} 环境: {ENGINE_ENV_NAMES[engine]}")
            env_dir, python_path = _ensure_canonical_environment(
                engine, selected_tool, python_version
            )
            print(f"   Python: {python_path}")
        if not python_path.exists():
            print(f"❌ 找不到 Python 可执行文件: {python_path}")
            return False, selected_tool, env_dir
        if existing and not force_reinstall:
            healthy, reason = managed_runtime_health(engine, python_path)
            if not healthy:
                force_reinstall = True
                print(f"🔧 检测到 {engine} 关键依赖损坏，将重新安装完整依赖: {reason}")
        before_versions = None
        try:
            before_versions = read_versions(python_path, engine)
        except Exception:
            pass
        sources = choose_install_sources(
            engine,
            selected_tool,
            python_path,
            requirements,
            preferred_index=preferred_index,
            network_timeout=network_timeout,
        )
        if not sources:
            print("❌ 没有可验证且能解析完整依赖的下载源。")
            _report_pdf2zh_fork_install_hint(engine, requirements)
            return False, selected_tool, env_dir
        if not _run_install(
            selected_tool,
            python_path,
            requirements,
            sources,
            reinstall=force_reinstall,
        ):
            _report_pdf2zh_fork_install_hint(engine, requirements)
            return False, selected_tool, env_dir
        if not validate_environment(
            engine,
            python_path,
            requirements,
            require_deepseek_thinking=require_deepseek_thinking,
        ):
            _report_pdf2zh_fork_install_hint(engine, requirements)
            return False, selected_tool, env_dir
        after_versions = read_versions(python_path, engine)
        print("\n✅ 翻译环境已更新。")
        if before_versions:
            print("   更新前:", format_versions(before_versions))
        print("   当前:", format_versions(after_versions))
        return True, selected_tool, env_dir
    except Exception as exc:
        print(f"\n⚠️ 翻译环境安装/更新失败: {exc}")
        _report_pdf2zh_fork_install_hint(engine, requirements)
        restored = find_existing_environment(engine, selected_tool)
        if restored:
            print("   仍将尝试使用当前翻译环境。如果翻译异常，请重新运行 python update_packages.py。")
        else:
            print("   当前没有可用翻译环境；可以稍后重新尝试。")
        return False, selected_tool, restored[1] if restored else None


def server_version_from_source() -> str:
    try:
        text = (SERVER_ROOT / "server.py").read_text(encoding="utf-8")
        match = re.search(
            r'^__version__\s*=\s*["\']([^"\']+)["\']', text, re.MULTILINE
        )
        return match.group(1) if match else "unknown"
    except Exception:
        return "unknown"


def package_update_state_path() -> Path:
    # 运行期状态与配置文件一并落在隐藏目录，避免污染 server/config
    return cache_dir() / "package_update_state.json"


def read_package_update_state() -> dict:
    try:
        with package_update_state_path().open("r", encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def write_package_update_state(server_version: str, status: str) -> None:
    path = package_update_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".json.tmp")
    with temp.open("w", encoding="utf-8") as handle:
        json.dump(
            {"server_version": server_version, "status": status},
            handle,
            ensure_ascii=False,
            indent=2,
        )
    os.replace(temp, path)


def _server_cli_bool(name: str, default: bool = False) -> bool:
    flag = "--" + name
    args = sys.argv[1:]
    for index, value in enumerate(args):
        if value == flag and index + 1 < len(args):
            return str(args[index + 1]).strip().lower() in {"1", "true", "yes", "y"}
        prefix = flag + "="
        if value.startswith(prefix):
            return value[len(prefix):].strip().lower() in {"1", "true", "yes", "y"}
    return default


def maybe_prompt_existing_user_update(
    *,
    env_tool: str = "auto",
    config_path: str | os.PathLike[str] | None = None,
    preferred_index: str | None = None,
) -> None:
    if not sys.stdin or not sys.stdin.isatty() or _server_cli_bool("enable_winexe"):
        return
    existing = find_existing_environment("pdf2zh_next", env_tool)
    if not existing:
        return
    server_version = server_version_from_source()
    state = read_package_update_state()
    if state.get("server_version") == server_version and state.get("status") in {
        "success",
        "declined",
        "failed",
        "skipped",
    }:
        return
    try:
        versions = read_versions(existing[2], "pdf2zh_next")
        current = versions.get("pdf2zh-next") or "unknown"
    except Exception:
        current = "unknown"
    if pdf2zh_next_meets_minimum(current):
        write_package_update_state(server_version, "skipped")
        print(
            f"✅ 当前 pdf2zh_next {current} 已满足 >=2.9.0，跳过启动时环境更新。"
        )
        return
    has_thinking = runtime_supports_deepseek_thinking(
        existing[2],
        env=managed_python_env(),
        isolated=True,
    )
    print("\n" + "─" * 60)
    print("🔄 检测到已有 Python 翻译环境")
    print(f"当前 pdf2zh_next: {current}")
    if has_thinking:
        print("当前环境已支持 DeepSeek V4 思考控制；仍可检查其他兼容依赖更新。")
    else:
        print(f"Zotero PDF2zh v{server_version} 的 DeepSeek V4 思考控制需要新版运行时。")
    print("更新会在当前 conda/uv 环境中直接安装依赖，不再创建 staging 或 backup 环境。")
    print("\n[Y] 检查并更新（推荐）")
    print("[N] 暂不更新")
    try:
        answer = input("\n选择 [Y/n]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = "n"
    if answer in {"", "y", "yes"}:
        success, _, _ = transactional_install_or_update(
            "pdf2zh_next",
            env_tool=existing[0],
            config_path=config_path,
            preferred_index=preferred_index,
            require_deepseek_thinking=True,
        )
        write_package_update_state(server_version, "success" if success else "failed")
        if not success:
            print(
                "⚠️ 本次更新未完成。"
                "本版本不会再次自动询问，可稍后运行 python update_packages.py 重试。"
            )
    else:
        write_package_update_state(server_version, "declined")
        print("👌 已保留当前翻译环境；本 Server 版本不会再次询问。")

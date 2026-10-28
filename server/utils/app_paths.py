"""程序根目录与运行期配置 / 缓存目录的定位。

用户不希望运行期配置散落在源码目录里，也不希望被自动更新覆盖。Server 端
因此把 ``config.json`` / ``config.toml`` / ``venv.json`` 从 ``server/config``
迁移到 ``<程序根>/config/pdf-cache``，并在 Windows 上把这两级目录设为隐藏；
``server/config`` 只保留随发布包分发的 ``*.example`` 模板。

本模块提供：

- :func:`app_root` —— 程序根目录，默认即 ``server.py`` 所在目录（源码布局），
  可通过环境变量 ``PDF2ZH_HOME`` 显式覆盖；
- :func:`install_root` —— Server 程序安装目录（``server.py`` 所在目录），
  **不受** ``PDF2ZH_HOME`` 影响，用于判定便携包是否自带运行时；
- :func:`bundled_runtime_dir` / :func:`bundled_python` /
  :func:`bundled_site_packages` —— 便携包内置运行时探测；
- :func:`cache_dir` —— 返回并准备好 ``<app_root>/config/pdf-cache``；
- :func:`ensure_hidden` —— 尽力把文件 / 目录标记为隐藏（Windows 打隐藏属性）；
- :func:`template_dir` —— 返回 ``<server>/config``，即 ``*.example`` 模板目录
  （随程序分发，不跟随 ``PDF2ZH_HOME``）；

- :func:`migrate_legacy_configs` —— 把旧版 ``server/config`` 下的运行期配置
  复制到新位置（只复制、不删除）。
"""

from __future__ import annotations

import logging
import os
import shutil
from collections.abc import Iterable
from pathlib import Path

logger = logging.getLogger(__name__)

#: 显式指定程序根目录的环境变量名（便携版启动器可用它覆盖自动推导）。
ENV_APP_HOME = "PDF2ZH_HOME"

#: 最终缓存 / 配置所在的子目录：``<app_root>/config/pdf-cache``。
_CONFIG_SUBDIR = "config"
_CACHE_SUBDIR = "pdf-cache"

#: Server 端的三个运行期配置文件名。对应的 ``*.example`` 模板仍在
#: ``<server>/config``，不随配置迁移。
CONFIG_FILE_NAMES = ("config.json", "config.toml", "venv.json")

#: 进程内缓存：避免每次调用都重复 mkdir / 设置隐藏属性。
#: 以 app_root 为 key，保证同一进程内不同根目录（含测试场景）互不串扰。
_cache_dir_cache: dict[Path, Path] = {}


def app_root() -> Path:
    """返回「程序所在文件夹的根目录」。

    推导规则（优先级从高到低）：

    1. 若环境变量 ``PDF2ZH_HOME`` 已设置，返回其 ``expanduser`` 后的绝对路径。
    2. 否则返回 ``server/`` 目录，即 ``server.py`` 所在目录（源码布局）：本文件
       位于 ``<server>/utils/app_paths.py``，取 ``utils`` 的上一级即可。

    这里刻意不做缓存：``PDF2ZH_HOME`` 可能在运行期被修改（例如测试），而路径
    推导本身开销极小。
    """
    env_home = os.environ.get(ENV_APP_HOME)
    if env_home:
        return Path(env_home).expanduser().resolve()

    # 源码布局：<server>/utils/app_paths.py -> <server>
    return Path(__file__).resolve().parent.parent


def template_dir() -> Path:
    """返回模板目录 ``<server>/config``（``*.example`` 保持原位）。

    模板随程序包分发、只读，因此刻意**不**跟随 ``PDF2ZH_HOME``：即使运行期
    配置被重定向到其它目录，模板仍从 Server 源码目录读取。该目录只存放
    ``*.example``；运行期配置文件在 :func:`cache_dir` 下。
    """
    return Path(__file__).resolve().parent.parent / _CONFIG_SUBDIR


def install_root() -> Path:
    """返回程序安装目录，即 ``server.py`` 所在目录（不随 ``PDF2ZH_HOME`` 变化）。

    与 :func:`app_root` 的区别：

    - ``app_root`` 是「运行期可写根」，用户可用 ``PDF2ZH_HOME`` 把配置 / 缓存
      重定向到别处；
    - ``install_root`` 是「程序文件实际所在目录」。便携包（``runtime/`` 与
      ``site-packages/``）随程序一起分发、自包含，因此探测内置运行时必须基于
      它，否则用户设置 ``PDF2ZH_HOME`` 后便携判定会丢失。
    """
    return Path(__file__).resolve().parent.parent


def bundled_runtime_dir() -> Path | None:
    """返回内置便携 Python 目录 ``<install_root>/runtime``；不存在返回 None。"""
    candidate = install_root() / "runtime"
    return candidate if candidate.is_dir() else None


def bundled_site_packages() -> Path | None:
    """返回便携依赖目录 ``<install_root>/site-packages``；不存在返回 None。"""
    candidate = install_root() / "site-packages"
    return candidate if candidate.is_dir() else None


def bundled_python() -> Path | None:
    """返回便携 Python 解释器；未打包或解释器缺失时返回 None。

    - Windows：``runtime/python.exe``；
    - 其它平台：``runtime/bin/python3``，再兜底 ``runtime/bin/python``。
    """
    runtime = bundled_runtime_dir()
    if runtime is None:
        return None
    if _is_windows():
        candidates = (runtime / "python.exe",)
    else:
        candidates = (runtime / "bin" / "python3", runtime / "bin" / "python")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def cache_dir() -> Path:
    """返回 ``<app_root>/config/pdf-cache``，确保目录存在且已设为隐藏。

    结果按 ``app_root`` 做进程内缓存，避免重复创建目录；若目录在运行期被外部
    删除，则自动重建，不会返回失效路径。
    """
    root = app_root()
    cached = _cache_dir_cache.get(root)
    if cached is not None and cached.is_dir():
        return cached

    target = root / _CONFIG_SUBDIR / _CACHE_SUBDIR
    target.mkdir(parents=True, exist_ok=True)
    # 需求要求最终落在隐藏目录中；对 config 与 pdf-cache 两级都尝试设置，
    # 使目录在文件管理器中默认不可见。
    ensure_hidden(target.parent)
    ensure_hidden(target)

    _cache_dir_cache[root] = target
    return target


def _is_windows() -> bool:
    """当前平台是否为 Windows（抽成函数便于测试替换，避免测试改动 os.name）。"""
    return os.name == "nt"


def ensure_hidden(path: str | os.PathLike[str]) -> None:
    """尽力把 ``path``（文件或目录）标记为隐藏，失败静默忽略。

    - Windows：用 ``ctypes`` 调用 ``kernel32.SetFileAttributesW``，在原有属性
      位上按位或上 ``FILE_ATTRIBUTE_HIDDEN``，不改动其它属性位；
    - 其它平台：Unix 无对应文件属性，保持 no-op（不使用点号前缀，以免改变用户
      指定的 ``config/pdf-cache`` 目录名）。

    任何异常都被吞掉：隐藏失败绝不能导致程序中断。
    """
    try:
        target = Path(path)
        if not target.exists():
            return
        if not _is_windows():
            return

        import ctypes

        FILE_ATTRIBUTE_HIDDEN = 0x02
        INVALID_FILE_ATTRIBUTES = 0xFFFFFFFF

        kernel32 = ctypes.windll.kernel32
        # GetFileAttributesW 默认 restype 是 c_int，0xFFFFFFFF 会被解释成 -1，
        # 与 INVALID_FILE_ATTRIBUTES 比较永远不成立；显式声明 uint32 才能正确
        # 识别「路径不存在 / 无权限」的失败返回。
        kernel32.GetFileAttributesW.argtypes = [ctypes.c_wchar_p]
        kernel32.GetFileAttributesW.restype = ctypes.c_uint32
        kernel32.SetFileAttributesW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32]
        kernel32.SetFileAttributesW.restype = ctypes.c_int

        attrs = kernel32.GetFileAttributesW(str(target))
        if attrs == INVALID_FILE_ATTRIBUTES:
            return
        if attrs & FILE_ATTRIBUTE_HIDDEN:
            return  # 已是隐藏，无需重复设置
        kernel32.SetFileAttributesW(str(target), attrs | FILE_ATTRIBUTE_HIDDEN)
    except Exception:  # noqa: BLE001 - best-effort，绝不因隐藏失败中断
        logger.debug("设置隐藏属性失败: %s", path, exc_info=True)


def migrate_legacy_configs(
    legacy_dir: str | os.PathLike[str],
    target_dir: str | os.PathLike[str] | None = None,
    names: Iterable[str] = CONFIG_FILE_NAMES,
) -> list[Path]:
    """把旧版配置从 ``legacy_dir`` 复制到新的运行期目录。

    仅当目标文件不存在、且旧文件存在时复制（``shutil.copy2`` 保留时间戳与
    权限）。刻意只复制、不移动：旧版 Server 或用户回滚时仍能读到原来的配置。
    单个文件复制失败只告警，不中断启动。

    Args:
        legacy_dir: 旧版运行期配置目录，通常是 ``<server>/config``。
        target_dir: 目标目录，默认 :func:`cache_dir`。
        names: 需要迁移的文件名，默认 ``config.json`` / ``config.toml`` /
            ``venv.json``。

    Returns:
        实际复制成功的文件路径列表。
    """
    target_root = Path(target_dir) if target_dir is not None else cache_dir()
    legacy_root = Path(legacy_dir)
    migrated: list[Path] = []

    for name in names:
        legacy = legacy_root / name
        target = target_root / name
        if not legacy.is_file() or target.exists():
            continue
        try:
            if legacy.resolve() == target.resolve():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(legacy, target)
        except OSError:
            logger.warning(
                "旧配置迁移失败: %s -> %s；将使用默认配置", legacy, target, exc_info=True
            )
            continue
        migrated.append(target)
        logger.info("已迁移旧配置: %s -> %s", legacy, target)

    return migrated
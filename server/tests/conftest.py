"""pytest 全局配置：把运行期配置 / 缓存目录隔离到临时目录。

``server.py`` 与 ``utils.environment_lifecycle`` 在导入阶段就会调用
``utils.app_paths.cache_dir()``（创建 ``<程序根>/config/pdf-cache``）。测试
绝不能向真实仓库或用户主目录写入运行期配置，因此在导入任何 server 模块
之前通过 ``PDF2ZH_HOME`` 把程序根重定向到一次性临时目录。

需要验证「默认程序根 = server 源码目录」的用例会自行清除该环境变量。
"""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile

_TEST_HOME = tempfile.mkdtemp(prefix="zotero-pdf2zh-test-home-")
os.environ["PDF2ZH_HOME"] = _TEST_HOME
atexit.register(shutil.rmtree, _TEST_HOME, ignore_errors=True)
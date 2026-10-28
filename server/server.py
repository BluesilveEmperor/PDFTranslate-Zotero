## server.py v1.0.0
# guaguastandup
# zotero-pdf2zh
import os
from flask import Flask, request, jsonify, send_file, Response
from werkzeug.serving import WSGIRequestHandler
import base64
import subprocess
import json, toml
import shutil
from pypdf import PdfReader
from utils.venv import VirtualEnvManager
from utils.environment_lifecycle import (
    find_existing_environment,
    format_versions,
    pdf2zh_next_meets_minimum,
    read_versions,
)
from utils.app_paths import cache_dir, ensure_hidden, migrate_legacy_configs
from utils.config import Config
from utils.config_map import pdf2zh_next_service_aliases
from utils.config_migration import prepare_config_files
from utils.cropper import Cropper
import traceback
import argparse
import sys  # 用于退出脚本
import re   # 用于解析版本号和提取错误信息
import io
import socket  # 用于端口检查
import time    # 用于 SSE 推送间隔
import threading
import uuid    # 用于生成任务唯一标识
from datetime import datetime  # 用于记录任务开始/结束时间
# 导入自动更新模块
from utils.auto_update import check_for_updates, fetch_and_show_notices, perform_update_optimized
# 导入任务管理器（用于 index.html 前端进度显示）
from utils.task_manager import task_manager
# 导入带进度解析的命令执行器
from utils.execute import execute_with_progress

_VALUE_ERROR_RE = re.compile(r'(?m)^ValueError:\s*(?P<msg>.+)$')

# 中间产物（上传源副本 + 内核输出 + 裁剪结果）统一写入 translated/.staging/<task_id>/，
# 成功后才 os.replace 到 translated 根；磁盘名与回传 fileList 名保持一致。
STAGING_DIRNAME = '.staging'

# 新命名前缀 -> 产物类型。长前缀必须排在短前缀之前，否则
# Traned_single_cut_ 会被 Traned_single_ 误吞。
_TRANED_PREFIX_RULES = (
    ('Traned_crop_compare_', 'crop-compare'),
    ('Traned_double_cut_', 'dual-cut'),
    ('Traned_single_cut_', 'mono-cut'),
    ('Traned_origin_cut_', 'origin-cut'),
    ('Traned_compare_', 'compare'),
    ('Traned_double_', 'dual'),
    ('Traned_single_', 'mono'),
)

# 剥前缀时同样按长前缀优先的顺序遍历。
_TRANED_PREFIXES = tuple(prefix for prefix, _ in _TRANED_PREFIX_RULES)

# 产物类型 -> 最终文件名前缀（对齐 PDFMathTranslate main 分支的 gui.py）。
_PRODUCT_PREFIXES = {
    'mono': 'Traned_single_',
    'dual': 'Traned_double_',
    'mono-cut': 'Traned_single_cut_',
    'dual-cut': 'Traned_double_cut_',
    'compare': 'Traned_compare_',
    'crop-compare': 'Traned_crop_compare_',
    'origin-cut': 'Traned_origin_cut_',
}


def strip_traned_prefix(stem):
    """剥掉已有的 Traned_* 前缀，返回语义 stem（前缀匹配大小写不敏感）。

    二次加工（cut / compare / crop-compare）的输入是 Zotero 回传的
    ``Traned_*`` 文件，若直接拼新前缀会产生
    ``Traned_single_Traned_double_x.pdf`` 之类的叠加名。

    与插件 ``classifyPdfFileName`` 保持一致：新前缀按小写比较，因此
    Windows 上被改成 ``traned_single_x.pdf`` / ``TRANED_DOUBLE_X.PDF`` 的
    文件同样能剥离出语义 stem。旧命名（``mono.pdf`` 等）仍保持大小写敏感。
    """
    text = str(stem)
    lowered = text.lower()
    for prefix in _TRANED_PREFIXES:
        if lowered.startswith(prefix.lower()):
            # 前缀均为 ASCII，lower() 不改变长度；按原长度切片保留原大小写。
            stripped = text[len(prefix):]
            return stripped or text
    return text


# 中间产物后缀：pdf2zh_next 内核产物自带 ``.mono`` / ``.dual`` / ``.LR_dual``
# 等标记，二次加工（cut / compare）时若不清洗，最终文件名会变成
# ``Traned_compare_paper.LR_dual.pdf``，既不符合命名约定，也会让
# /translatedInfo?stem=paper 的过滤失效。
_INTERMEDIATE_SUFFIXES = (
    '.no_watermark',
    '.mono-cut',
    '.dual-cut',
    '.crop-compare',
    '.compare',
    '.LR_dual',
    '.TB_dual',
    '.mono',
    '.dual',
    '.cut',
)


def semantic_stem(path):
    """从文件名得到语义 stem：去掉扩展名、``Traned_*`` 前缀与布局/中间产物标记。"""
    stem = os.path.splitext(os.path.basename(str(path)))[0]
    stem = strip_traned_prefix(stem)
    for suffix in _INTERMEDIATE_SUFFIXES:
        if len(stem) > len(suffix) and stem.endswith(suffix):
            return stem[:-len(suffix)]
    return stem


def match_traned_product(name):
    """按 ``Traned_*`` 前缀返回产物类型；不是合法产物命名时返回 ``None``。

    与插件侧 ``classifyPdfFileName`` 保持同一契约：新前缀大小写不敏感；前缀后
    必须还有非空 stem 且以 ``.pdf`` 结尾，否则 ``Traned_single_.pdf`` 这类残缺
    命名会回落为原文。旧命名（``mono.pdf`` 等）由 :meth:`get_filetype` 处理，
    仍保持大小写敏感。
    """
    text = str(name)
    lowered = text.lower()
    for prefix, outtype in _TRANED_PREFIX_RULES:
        if not lowered.startswith(prefix.lower()):
            continue
        # 前缀均为 ASCII，lower() 不改变长度；用原名同位置切片取 remainder，
        # 保留 stem 的原始大小写。
        remainder = text[len(prefix):]
        if not remainder.lower().endswith('.pdf'):
            return None
        return outtype if remainder[:-len('.pdf')] else None
    return None


def translated_filename(stem, outtype):
    """按产物类型生成最终文件名（例如 ``Traned_single_cut_x.pdf``）。"""
    prefix = _PRODUCT_PREFIXES.get(outtype)
    if not prefix:
        raise ValueError(f"未知产物类型: {outtype}")
    return f"{prefix}{strip_traned_prefix(stem)}.pdf"


def task_staging_dir(task_id):
    """创建并返回某个任务的 staging 子目录。

    每个任务一个子目录，避免并发翻译互相覆盖；``.staging`` 本身在 Windows
    上设为隐藏。``task_id`` 会被清洗成安全的单层目录名。
    """
    safe_id = re.sub(r'[^A-Za-z0-9_.-]+', '_', str(task_id or '')).strip('._')
    path = os.path.join(output_folder, STAGING_DIRNAME, safe_id[:64] or 'task')
    os.makedirs(path, exist_ok=True)
    ensure_hidden(os.path.join(output_folder, STAGING_DIRNAME))
    return path


def cleanup_staging(staging_dir):
    """删除某任务的 staging 子目录，容忍 Windows 文件锁。

    只删除 ``<output>/.staging/`` 下的直接子目录，避免误删输出根。允许目录
    残留为空目录，但不应残留文件。
    """
    try:
        path = os.path.abspath(str(staging_dir))
        if os.path.basename(os.path.dirname(path)) != STAGING_DIRNAME:
            return
        if not os.path.isdir(path):
            return
        shutil.rmtree(path, ignore_errors=True)
        if not os.path.isdir(path):
            return
        # 兜底：rmtree 可能因文件锁放弃，逐个删除残余文件。
        for name in os.listdir(path):
            child = os.path.join(path, name)
            try:
                if os.path.isdir(child):
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    os.remove(child)
            except OSError:
                pass
        try:
            os.rmdir(path)
        except OSError:
            pass
    except Exception:  # noqa: BLE001 - 清理失败绝不能影响已完成的翻译结果
        pass


def move_products_to_output(products, stem):
    """把 staging 内的最终产物移动到 translated 根并套用 Traned_* 命名。

    ``products`` 为 ``[(staging_path, outtype), ...]``。``os.replace`` 原子
    覆盖，重复翻译同名文件时不会残留旧产物。

    Windows 上目标文件可能被 Zotero / 预览器占用，``os.replace`` 失败后改用
    ``shutil.copy2`` 复制到同目录临时文件再 ``os.replace``：最终替换失败时旧
    target 保持原样，不会被截断。仍然失败则抛 ``RuntimeError``，让任务走失败
    路径，绝不静默返回 success 而缺失 fileList 文件。
    """
    moved = []
    for path, outtype in products:
        if not os.path.isfile(path):
            continue
        target = os.path.join(output_folder, translated_filename(stem, outtype))
        try:
            os.replace(path, target)
        except OSError as exc:
            print(f"⚠️ [Zotero PDF2zh Server] os.replace 归档失败，改用复制: {path} -> {target}: {exc}")
            tmp_target = target + '.tmp'
            try:
                # 先复制到同目录临时文件：最终替换失败时旧 target 不会被截断。
                shutil.copy2(path, tmp_target)
                os.replace(tmp_target, target)
            except OSError as copy_exc:
                try:
                    os.remove(tmp_target)
                except OSError:
                    pass  # 临时文件可能尚未创建或已被移走
                print(f"❌ [Zotero PDF2zh Server] 产物归档失败: {path} -> {target}: {copy_exc}")
                raise RuntimeError(
                    f"产物归档失败: {path} -> {target}: {copy_exc}"
                ) from copy_exc
        moved.append(target)
    return moved

__version__ = "1.0.0"
update_log = "首发 1.0.0：配置迁至 config/pdf-cache；产物统一 Traned_single_/Traned_double_ 命名，中间产物进 translated/.staging；pdf2zh 依赖改走 fork git 源（gh-proxy 加速）。请同时更新插件和 Server。"

############# config file #########
pdf2zh      = 'pdf2zh'
pdf2zh_next = 'pdf2zh_next'
venv        = 'venv'

# TODO: 强制设置标准输出和标准错误的编码为 UTF-8
# sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
# sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

# Windows 下防止子进程弹出控制台窗口
if sys.platform == 'win32':
    CREATE_NO_WINDOW = subprocess.CREATE_NO_WINDOW
else:
    CREATE_NO_WINDOW = 0

# 所有系统: 获取当前脚本server.py所在的路径（源码布局下的程序根目录）
root_path     = os.path.dirname(os.path.abspath(__file__))
# 模板目录: 仅存放随发布包分发的 *.example, 保持原位; 运行期配置不写这里
config_folder = os.path.join(root_path, 'config')
# 运行期配置目录: <程序根>/config/pdf-cache
# 默认即 <server>/config/pdf-cache, 可用环境变量 PDF2ZH_HOME 覆盖程序根
runtime_config_folder = str(cache_dir())
output_folder = os.path.join(root_path, 'translated')
config_path = { # 配置文件路径（均位于隐藏的运行期配置目录）
    pdf2zh:      os.path.join(runtime_config_folder, 'config.json'),
    pdf2zh_next: os.path.join(runtime_config_folder, 'config.toml'),
    venv:        os.path.join(runtime_config_folder, 'venv.json'),
}

######### venv config #########
venv_name = { # venv名称
    pdf2zh:      'zotero-pdf2zh-venv',
    pdf2zh_next: 'zotero-pdf2zh-next-venv',
}

default_env_tool = 'auto' # 自动沿用已有 uv/conda；新环境优先 uv
enable_venv = True

PORT = 8890     # 默认端口号


class _QuietAccessHandler(WSGIRequestHandler):
    # 进度查询很勤，默认访问日志会插进 tqdm/rich 进度条中间。
    _SILENT_PREFIXES = (
        '/api/tasks',
        '/api/history',
        '/events',
        '/health',
        '/favicon',
    )

    def log_request(self, code='-', size='-'):
        path = (getattr(self, 'path', None) or '').split('?', 1)[0]
        if any(path == prefix or path.startswith(prefix + '/') for prefix in self._SILENT_PREFIXES):
            return
        super().log_request(code, size)


class PDFTranslator:

    def __init__(self, args):
        self.app = Flask(__name__)
        if args.enable_venv:
            self.env_manager = VirtualEnvManager(config_path[venv], venv_name, args.env_tool, args.enable_mirror, args.skip_install, args.mirror_source)
        self.cropper = Cropper()
        self.setup_routes()

    def setup_routes(self):
        # 新增：首页路由 - 提供 index.html 前端进度监控页面
        self.app.add_url_rule('/', 'index', self.index)

        self.app.add_url_rule('/translate', 'translate', self.translate, methods=['POST'])
        self.app.add_url_rule('/crop', 'crop', self.crop, methods=['POST'])
        self.app.add_url_rule('/crop-compare', 'crop-compare', self.crop_compare, methods=['POST'])
        self.app.add_url_rule('/compare', 'compare', self.compare, methods=['POST'])
        self.app.add_url_rule(
            '/translatedFile/<path:filename>',
            'download',
            self.download_file,
            methods=['GET', 'HEAD'],
        )
        self.app.add_url_rule(
            '/translatedInfo',
            'translated_info',
            self.translated_info,
            methods=['GET'],
        )

        # 新增：健康检查端点 - 用于检查服务器状态
        self.app.add_url_rule('/health', 'health', self.health_check)
        # 新增：SSE 端点 - 实时推送翻译进度给 index.html 前端
        self.app.add_url_rule('/events', 'events', self.events)
        # 新增：历史记录 API - 供 index.html 前端获取翻译历史
        self.app.add_url_rule('/api/history', 'history', self.get_history)
        self.app.add_url_rule('/api/tasks', 'tasks', self.get_tasks)
        # 新增：配置信息 API - 供 index.html 前端显示当前服务配置
        self.app.add_url_rule('/api/config', 'config', self.get_config)
        # 新增：favicon 路由
        self.app.add_url_rule('/favicon.svg', 'favicon', self.favicon)
        # 新增：提示音音频路由
        self.app.add_url_rule('/bo.mp3', 'notification_sound', self.notification_sound)

    ##################################################################
    # 健康检查端点 /health - 检查服务器状态
    # 返回JSON格式的服务器状态信息，包括状态码、版本号和消息
    ##################################################################
    def health_check(self):
        return jsonify({
            'status': 'ok',
            'version': __version__,
            'message': 'PDF2zh Server is running',
            'outputDir': os.path.abspath(output_folder),
        }), 200

    ##################################################################
    # 首页路由 / - 提供 index.html 前端进度监控页面
    ##################################################################
    def index(self):
        try:
            index_path = os.path.join(root_path, 'index.html')
            if os.path.exists(index_path):
                return send_file(index_path)
            else:
                return jsonify({'status': 'error', 'message': 'index.html not found'}), 404
        except Exception as e:
            return jsonify({'status': 'error', 'message': str(e)}), 500

    ##################################################################
    # SSE (Server-Sent Events) 端点 /events - 实时推送翻译进度给前端
    # index.html 通过 EventSource('/events') 接收数据
    ##################################################################
    def events(self):
        def generate():
            while True:
                try:
                    tasks_data = {
                        'type': 'tasks',
                        'data': task_manager.get_active_tasks_list()
                    }
                    yield f"data: {json.dumps(tasks_data)}\n\n"
                    time.sleep(1)  # 每秒推送一次
                except GeneratorExit:
                    break
        return Response(
            generate(),
            mimetype='text/event-stream',
            headers={
                'Cache-Control': 'no-cache, no-store',
                'X-Accel-Buffering': 'no',
            },
        )

    ##################################################################
    # 历史记录 API /api/history - 供 index.html 前端获取翻译历史
    ##################################################################
    def get_history(self):
        return jsonify({'status': 'success', 'history': task_manager.get_history()})

    def get_tasks(self):
        return jsonify({'status': 'success', 'tasks': task_manager.get_active_tasks_list()})

    ##################################################################
    # 配置信息 API /api/config - 供 index.html 前端显示当前服务配置
    ##################################################################
    def get_config(self):
        config_info = {
            'version': __version__,
            'port': args.port,
            'enable_venv': args.enable_venv,
            'env_tool': args.env_tool,
            'enable_mirror': args.enable_mirror,
            'mirror_source': args.mirror_source if args.enable_mirror else '-',
            'skip_install': args.skip_install,
            'enable_winexe': args.enable_winexe,
        }
        return jsonify({'status': 'success', 'config': config_info})

    ##################################################################
    # Favicon 路由
    ##################################################################
    def favicon(self):
        favicon_path = os.path.join(root_path, 'favicon.svg')
        if os.path.exists(favicon_path):
            return send_file(favicon_path, mimetype='image/svg+xml')
        return '', 404

    ##################################################################
    # 提示音音频路由
    ##################################################################
    def notification_sound(self):
        sound_path = os.path.join(root_path, 'bo.mp3')
        if os.path.exists(sound_path):
            return send_file(sound_path, mimetype='audio/mpeg')
        return '', 404

    ##################################################################
    @staticmethod
    def _safe_upload_filename(raw_name):
        if not isinstance(raw_name, str):
            raise ValueError("Invalid PDF filename")
        name = raw_name.strip()
        if (
            not name
            or name in {'.', '..'}
            or '/' in name
            or '\\' in name
            or os.path.basename(name) != name
        ):
            raise ValueError("Invalid PDF filename")
        if not name.lower().endswith('.pdf'):
            raise ValueError("Only PDF uploads are accepted")
        return name

    def process_request(self, task_id=None):
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise ValueError("Invalid JSON request")
        config = Config(data)

        file_name = self._safe_upload_filename(data.get('fileName'))

        file_content = data.get('fileContent', '')
        if not isinstance(file_content, str):
            raise ValueError("Invalid PDF content")
        if file_content.startswith('data:application/pdf;base64,'):
            file_content = file_content[len('data:application/pdf;base64,'):]
        try:
            decoded = base64.b64decode(file_content)
        except Exception as exc:
            raise ValueError(f"Invalid PDF content: {exc}") from exc

        # 所有校验通过后才创建 staging 目录：非法请求不能在 translated/.staging
        # 留下副作用（空任务目录）。
        # 上传源副本写入该任务的 staging 子目录，绝不落到 translated 根；
        # 未提供 task_id 时退回输出根（兼容直接调用）。
        base = os.path.abspath(task_staging_dir(task_id) if task_id else output_folder)
        input_path = os.path.abspath(os.path.join(base, file_name))
        try:
            if os.path.commonpath([base, input_path]) != base:
                raise ValueError("Invalid PDF filename")
        except ValueError:
            if task_id:
                cleanup_staging(base)
            raise ValueError("Invalid PDF filename")

        try:
            with open(input_path, 'wb') as f:
                f.write(decoded)
        except OSError as exc:
            # 磁盘满 / 权限不足：调用方此时尚未拿到 input_path，若不在这里清理，
            # translated/.staging/ 会留下空任务目录（或半截文件）。
            if task_id:
                cleanup_staging(base)
            raise ValueError(f"无法保存上传的 PDF: {exc}") from exc

        return input_path, config

    def _existing_output_files(self, paths):
        existing = []
        for path in paths or []:
            if path and os.path.exists(path):
                existing.append(os.path.abspath(path))
        return existing

    def _success_files_payload(self, paths):
        existing = self._existing_output_files(paths)
        return {
            'status': 'success',
            'fileList': [os.path.basename(p) for p in existing],
            'outputDir': os.path.abspath(output_folder),
            'filePaths': existing,
        }

    def _success_files_response(self, paths):
        return jsonify(self._success_files_payload(paths)), 200

    def _build_task_info(self, task_id, input_path, config, engine, start_time, status='开始处理'):
        output_types = []
        if config.mono: output_types.append('mono')
        if config.dual: output_types.append('dual')
        if config.mono_cut: output_types.append('mono-cut')
        if config.dual_cut: output_types.append('dual-cut')
        if config.compare: output_types.append('compare')
        if config.crop_compare: output_types.append('crop-compare')
        config_summary = {
            'sourceLang': config.sourceLang,
            'targetLang': config.targetLang,
            'outputTypes': output_types,
        }
        if engine == pdf2zh:
            config_summary['threadNum'] = config.thread_num
            config_summary['babeldoc'] = config.babeldoc
        elif engine == pdf2zh_next:
            config_summary['qps'] = config.qps
            config_summary['dualMode'] = config.dual_mode
            config_summary['noWatermark'] = config.no_watermark
            config_summary['ocr'] = config.ocr or config.auto_ocr
            config_summary['poolSize'] = config.pool_size
        if config.skip_last_pages and config.skip_last_pages > 0:
            config_summary['skipLastPages'] = config.skip_last_pages
        if config.no_watermark:
            config_summary['noWatermark'] = config.no_watermark

        model_name = config.llm_api.get('model', '')
        service = config.service
        if not model_name:
            if service == 'siliconflowfree':
                model_name = 'siliconflowfree (免费服务)'
            elif service == 'bing':
                model_name = 'Bing 翻译'
            elif service == 'google':
                model_name = 'Google 翻译'
            else:
                model_name = f'{service} (默认模型)'

        return {
            'taskId': task_id,
            'active': True,
            'finished': False,
            'fileName': os.path.basename(input_path),
            'engine': engine,
            'service': config.service,
            'modelName': model_name,
            'startTime': start_time.isoformat(),
            'progress': 0,
            'status': status,
            'message': '正在初始化...',
            'config': config_summary,
        }

    @staticmethod
    def _truthy_flag(value):
        if value is True:
            return True
        if value is False or value is None:
            return False
        return str(value).strip().lower() in {'1', 'true', 'yes', 'async', 'accepted'}

    def _client_wants_async_job(self):
        # 新插件会带 asyncJob=true 或 X-PDF2zh-Protocol: accepted。
        # 旧官方插件 / DCC 4.0.3 都没有这个标记，必须同步返回 success+fileList，
        # 否则它们会把 accepted 当成失败，条目下挂不上文件。
        header = (request.headers.get('X-PDF2zh-Protocol') or '').strip().lower()
        if header in {'accepted', 'async'}:
            return True
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict):
            return False
        if 'asyncJob' in data:
            return self._truthy_flag(data.get('asyncJob'))
        protocol = str(data.get('clientProtocol') or '').strip().lower()
        return protocol in {'accepted', 'async'}

    def _start_accepted_job(self, task_id, task_info, worker, context):
        # 新插件：POST 立刻 accepted，翻完后按 taskId 取结果，避免 Windows 长连接被掐。
        # 旧插件：阻塞到完成，再返回 {status: success, fileList, ...}。
        task_manager.add_task(task_id, task_info)
        if self._client_wants_async_job():
            def run():
                try:
                    worker()
                except Exception as exc:
                    task_manager.complete_task(task_id, 'failed', str(exc), error=str(exc))
                    self._exception_payload(exc, context=context)

            threading.Thread(target=run, daemon=True).start()
            return jsonify({'status': 'accepted', 'taskId': task_id}), 200

        print(f"ℹ️ [Zotero PDF2zh Server] 旧插件协议：同步等待完成后返回 fileList ({context})")
        try:
            payload = worker() or {}
            if payload.get('status') == 'error':
                return jsonify(payload), 500
            if payload.get('status') != 'success':
                return jsonify({
                    'status': 'error',
                    'message': payload.get('message') or '操作失败，请查看详细日志。',
                }), 500
            return jsonify(payload), 200
        except Exception as exc:
            task_manager.complete_task(task_id, 'failed', str(exc), error=str(exc))
            return self._handle_exception(exc, context=context)

    def _complete_job_files(self, task_id, paths, message):
        payload = self._success_files_payload(paths)
        if not payload['fileList']:
            task_manager.complete_task(task_id, 'failed', '操作失败，请查看详细日志。', error='无文件生成')
            return {'status': 'error', 'message': '操作失败，请查看详细日志。'}
        task_manager.complete_task(
            task_id,
            'success',
            message,
            file_list=payload['fileList'],
            file_paths=payload['filePaths'],
            output_dir=payload['outputDir'],
            result=payload,
        )
        return payload

    def translated_info(self):
        # 列出 translated 目录里的 PDF，供网页预览 / 排障使用
        try:
            stem = (request.args.get('stem') or '').strip()
            base = os.path.abspath(output_folder)
            files = []
            if os.path.isdir(base):
                for name in os.listdir(base):
                    if not name.lower().endswith('.pdf'):
                        continue
                    if stem and not (
                        name.startswith(stem + '-')
                        or name.startswith(stem + '.')
                        or name.startswith(stem + '_')
                        # 新命名 Traned_*_<stem>.pdf 也应按语义 stem 命中
                        or (name.startswith('Traned_') and semantic_stem(name) == stem)
                    ):
                        continue
                    full = os.path.abspath(os.path.join(base, name))
                    try:
                        if os.path.commonpath([base, full]) != base:
                            continue
                    except ValueError:
                        continue
                    if not os.path.isfile(full):
                        continue
                    files.append({
                        'fileName': name,
                        'filePath': full,
                        'size': os.path.getsize(full),
                        'mtime': os.path.getmtime(full),
                    })
            files.sort(key=lambda item: item['mtime'], reverse=True)
            return jsonify({
                'status': 'ok',
                'outputDir': base,
                'files': files,
            }), 200
        except Exception as e:
            traceback.print_exc()
            return jsonify({'status': 'error', 'message': str(e)}), 500

    # 下载文件 /translatedFile/<filename>
    # 支持 ?preview=true 参数用于 index.html 的在线预览功能
    def download_file(self, filename):
        try:
            # Flask/Werkzeug has already decoded the route parameter once.
            # Decoding again would reinterpret literal names such as "%2F.pdf"
            # and break cross-platform downloads.
            filename = os.path.basename(filename or '')
            if not filename or filename in {'.', '..'}:
                return jsonify({'status': 'error', 'message': 'Invalid path'}), 400
            base = os.path.abspath(output_folder)
            full = os.path.abspath(os.path.join(output_folder, filename))
            # 防止目录穿越
            if os.path.commonpath([base, full]) != base:
                return jsonify({'status': 'error', 'message': 'Invalid path'}), 400

            if os.path.exists(full):
                # 如果 preview=true，则以内联方式返回（用于浏览器内预览）
                is_preview = request.args.get('preview') == 'true'
                return send_file(full, as_attachment=not is_preview)
            # 新增：不存在时明确返回 404，而不是什么都不返回
            return jsonify({'status': 'error', 'message': f'File not found: {filename}'}), 404
        except Exception as e:
            traceback.print_exc()
            return jsonify({'status': 'error', 'message': str(e)}), 500

    ############################# 核心逻辑 #############################
    # 翻译 /translate
    def translate(self):
        # 生成任务ID并记录开始时间（用于 index.html 前端进度显示）
        task_id = str(uuid.uuid4())
        start_time = datetime.now()
        input_path = None
        handed_off = False

        try:
            input_path, config = self.process_request(task_id)
            infile_type = self.get_filetype(input_path)
            engine = config.engine
            task_info = self._build_task_info(
                task_id, input_path, config, engine, start_time, status='开始翻译'
            )

            if infile_type != 'origin':
                return jsonify({'status': 'error', 'message': 'Input file must be an original PDF file.'}), 400

            payload = self._start_accepted_job(
                task_id,
                task_info,
                lambda: self._execute_translate_job(task_id, input_path, config, engine),
                '/translate',
            )
            # job 自己负责清理 staging（含异步线程路径）
            handed_off = True
            return payload
        except Exception as e:
            task_manager.complete_task(task_id, 'failed', str(e), error=str(e))
            return self._handle_exception(e, context='/translate')
        finally:
            if not handed_off and input_path:
                cleanup_staging(os.path.dirname(input_path))

    def _execute_translate_job(self, task_id, input_path, config, engine):
        """翻译任务出口：成功才归档产物，任何结果都清理 staging。"""
        stem = semantic_stem(input_path)
        staging_dir = os.path.dirname(input_path)
        try:
            products = self._translate_products(task_id, input_path, config, engine)
            final_paths = move_products_to_output(products, stem)
            for f in final_paths:
                size = os.path.getsize(f)
                print(f"🐲 翻译成功, 生成文件: {f}, 大小为: {size/1024.0/1024.0:.2f} MB")

            if not final_paths:
                task_manager.complete_task(task_id, 'failed', '操作失败，请查看详细日志。', error='无文件生成')
                return {'status': 'error', 'message': '操作失败，请查看详细日志。'}

            payload = self._success_files_payload(final_paths)
            task_manager.complete_task(
                task_id,
                'success',
                f'成功生成 {len(final_paths)} 个文件',
                file_list=payload['fileList'],
                file_paths=payload['filePaths'],
                output_dir=payload['outputDir'],
                result=payload,
            )
            return payload
        finally:
            cleanup_staging(staging_dir)

    def _translate_products(self, task_id, input_path, config, engine):
        """执行翻译与后处理，返回 staging 内的 [(path, outtype)] 列表。"""
        stem = semantic_stem(input_path)
        products = []

        if engine == pdf2zh:
            print("🔍 [Zotero PDF2zh Server] PDF2zh 开始翻译文件...")
            mono_path, dual_path = self.translate_pdf(input_path, config, task_id)
            products.append((mono_path, 'mono'))
            products.append((dual_path, 'dual'))
            if config.mono_cut:
                mono_cut_path = self.get_filename_after_process(mono_path, 'mono-cut', engine, stem=stem)
                self.cropper.crop_pdf(config, mono_path, 'mono', mono_cut_path, 'mono-cut')
                products.append((mono_cut_path, 'mono-cut'))
            if config.dual_cut:
                dual_cut_path = self.get_filename_after_process(dual_path, 'dual-cut', engine, stem=stem)
                self.cropper.crop_pdf(config, dual_path, 'dual', dual_cut_path, 'dual-cut')
                products.append((dual_cut_path, 'dual-cut'))
            if config.crop_compare:
                crop_compare_path = self.get_filename_after_process(dual_path, 'crop-compare', engine, stem=stem)
                self.cropper.crop_pdf(config, dual_path, 'dual', crop_compare_path, 'crop-compare')
                products.append((crop_compare_path, 'crop-compare'))
            if config.compare and config.babeldoc == False: # babeldoc不支持compare
                compare_path = self.get_filename_after_process(dual_path, 'compare', engine, stem=stem)
                self.cropper.merge_pdf(dual_path, compare_path)
                products.append((compare_path, 'compare'))

        elif engine == pdf2zh_next:
            print("🔍 [Zotero PDF2zh Server] PDF2zh_next 开始翻译文件...")
            if config.mono_cut or config.mono:
                config.no_mono = False
            if config.dual or config.dual_cut or config.crop_compare or config.compare:
                config.no_dual = False

            if config.no_dual and config.no_mono:
                raise ValueError("⚠️ [Zotero PDF2zh Server] pdf2zh_next 引擎至少需要生成 mono 或 dual 文件, 请检查 no_dual 和 no_mono 配置项")


            retList = self.translate_pdf_next(input_path, config, task_id)

            if config.no_mono:
                dual_path = retList[0]
            elif config.no_dual:
                mono_path = retList[0]
                products.append((mono_path, 'mono'))
            else:
                mono_path, dual_path = retList[0], retList[1]
                products.append((mono_path, 'mono'))

            # Canonicalize every pdf2zh_next dual output so later operations
            # can recover its layout even after the file is attached to Zotero
            # and uploaded again in a separate request.
            primary_dual_path = None
            LR_dual_path = None
            TB_dual_path = None
            if not config.no_dual:
                primary_dual_path = self._canonicalize_pdf2zh_next_dual(dual_path, config.dual_mode)
                if config.dual_mode == 'LR':
                    LR_dual_path = primary_dual_path
                    if config.dual_cut or config.crop_compare:
                        _, TB_dual_path = self.cropper.pdf_dual_mode(primary_dual_path, 'LR', 'TB')
                else:
                    TB_dual_path = primary_dual_path

                if config.dual:
                    products.append((primary_dual_path, 'dual'))

            if config.mono_cut:
                mono_cut_path = self.get_filename_after_process(mono_path, 'mono-cut', engine, stem=stem)
                self.cropper.crop_pdf(config, mono_path, 'mono', mono_cut_path, 'mono-cut')
                products.append((mono_cut_path, 'mono-cut'))

            if config.dual_cut:
                if not TB_dual_path:
                    raise ValueError("dual-cut 需要 TB dual 输入，但未能准备该布局。")
                dual_cut_path = self.get_filename_after_process(TB_dual_path, 'dual-cut', engine, stem=stem)
                self.cropper.crop_pdf(config, TB_dual_path, 'dual', dual_cut_path, 'dual-cut')
                products.append((dual_cut_path, 'dual-cut'))

            if config.crop_compare:
                if not TB_dual_path:
                    raise ValueError("crop-compare 需要 TB dual 输入，但未能准备该布局。")
                crop_compare_path = self.get_filename_after_process(TB_dual_path, 'crop-compare', engine, stem=stem)
                self.cropper.crop_pdf(config, TB_dual_path, 'dual', crop_compare_path, 'crop-compare')
                products.append((crop_compare_path, 'crop-compare'))

            if config.compare:
                if config.dual_mode == 'LR':
                    if not LR_dual_path:
                        raise ValueError("compare 需要 LR dual 输入，但未能准备该布局。")
                    compare_path = self.get_filename_after_process(LR_dual_path, 'compare', engine, stem=stem)
                    if os.path.exists(compare_path):
                        os.remove(compare_path)
                    shutil.copyfile(LR_dual_path, compare_path)
                    products.append((compare_path, 'compare'))
                else:
                    if not TB_dual_path:
                        raise ValueError("compare 需要 TB dual 输入，但未能准备该布局。")
                    compare_path = self.get_filename_after_process(TB_dual_path, 'compare', engine, stem=stem)
                    self.cropper.merge_pdf(TB_dual_path, compare_path)
                    products.append((compare_path, 'compare'))
        else:
            raise ValueError(f"⚠️ [Zotero PDF2zh Server] 输入了不支持的翻译引擎: {engine}, 目前脚本仅支持: pdf2zh/pdf2zh_next")

        return products

    def _handle_exception(self, exc, status_code=500, context=None):
        return jsonify(self._exception_payload(exc, context=context)), status_code

    def _exception_payload(self, exc, context=None):
        if context:
            print(f"⚠️ [Zotero PDF2zh Server] {context} Error: {exc}")
        else:
            print(f"⚠️ [Zotero PDF2zh Server] Error: {exc}")
        traceback.print_exception(type(exc), exc, exc.__traceback__)
        info = self._derive_error_info(exc)
        payload = {
            'status': 'error',
            'ok': False,
            'message': info['message'],
        }
        error_type = info.get('errorType')
        if error_type:
            payload['errorType'] = error_type
        if isinstance(exc, subprocess.CalledProcessError):
            payload['exitCode'] = exc.returncode
        return payload

    def _derive_error_info(self, exc):
        parts = []
        if isinstance(exc, subprocess.CalledProcessError) and getattr(exc, 'stderr', None):
            parts.append(exc.stderr)
        formatted = ''.join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        if formatted:
            parts.append(formatted)
        blob = '\n'.join(part for part in parts if part)

        ve_msg = self._extract_value_error(blob)
        if ve_msg:
            return {
                'errorType': 'ValueError',
                'message': ve_msg,
            }

        def _tail_readable(text):
            lines = [ln.rstrip() for ln in text.splitlines()]
            for ln in reversed(lines):
                if not ln:
                    continue
                if ln.startswith(('Traceback', 'File ')):
                    continue
                return ln
            return str(exc).strip() or exc.__class__.__name__

        fallback_message = _tail_readable(blob) if blob else (str(exc).strip() or exc.__class__.__name__)
        return {
            'errorType': exc.__class__.__name__,
            'message': fallback_message,
        }

    @staticmethod
    def _extract_value_error(blob):
        if not blob:
            return None
        if not isinstance(blob, str):
            blob = str(blob)

        matches = list(_VALUE_ERROR_RE.finditer(blob))
        if not matches:
            return None

        match = matches[-1]
        msg = match.group('msg').strip()

        tail_lines = []
        for line in blob[match.end():].splitlines():
            if not line:
                break
            if line.startswith('Traceback') or _VALUE_ERROR_RE.match(line):
                break
            if line[:1] in (' ', '\t') or line.startswith('^'):
                tail_lines.append(line.strip())
            else:
                break

        if tail_lines:
            msg += ' ' + ' '.join(tail_lines)

        return msg or None

    # 裁剪 /crop
    def crop(self):
        task_id = str(uuid.uuid4())
        input_path = None
        try:
            input_path, config = self.process_request(task_id)
            stem = semantic_stem(input_path)
            infile_type = self.get_filetype(input_path)

            source_path = input_path
            if infile_type == 'dual' and self.get_dual_mode(input_path, config.dual_mode) == 'LR':
                # Crop means a crop result, not merely a layout conversion.
                # Normalize LR -> alternating-page TB internally, then continue
                # through the normal dual -> dual-cut operation.
                _, source_path = self.cropper.pdf_dual_mode(input_path, 'LR', 'TB')

            new_type = self.get_filetype_after_crop(input_path)
            if new_type == 'unknown':
                return jsonify({
                    'status': 'error',
                    'errorType': 'InvalidPDFOperation',
                    'message': f'当前 PDF 类型 {infile_type} 不能再次执行裁剪。请选择原文、mono 或 dual 文件。'
                }), 400

            new_path = self.get_filename_after_process(input_path, new_type, config.engine, stem=stem)
            self.cropper.crop_pdf(config, source_path, infile_type, new_path, new_type)
            print(f"🔍 [Zotero PDF2zh Server] 开始裁剪文件: {source_path}, {infile_type}, 裁剪类型: {new_type}, {new_path}")

            final_paths = move_products_to_output([(new_path, new_type)], stem)
            if final_paths:
                return self._success_files_response(final_paths)
            return jsonify({'status': 'error', 'message': f'Crop failed: {new_path} not found'}), 500
        except Exception as e:
            return self._handle_exception(e, context='/crop')
        finally:
            if input_path:
                cleanup_staging(os.path.dirname(input_path))

    def crop_compare(self):
        task_id = str(uuid.uuid4())
        start_time = datetime.now()
        input_path = None
        handed_off = False
        try:
            input_path, config = self.process_request(task_id)
            infile_type = self.get_filetype(input_path)
            engine = config.engine

            if infile_type == 'crop-compare':
                return jsonify({
                    'status': 'error',
                    'errorType': 'InvalidPDFOperation',
                    'message': '该 PDF 已经是“裁剪后双语对照”结果，无需再次执行 crop-compare。请选择原文或 dual 附件。'
                }), 409
            if infile_type not in {'origin', 'dual', 'dual-cut'}:
                return jsonify({
                    'status': 'error',
                    'errorType': 'InvalidPDFOperation',
                    'message': f'当前 PDF 类型 {infile_type} 不能执行 crop-compare。请选择原文、dual 或 dual-cut 文件。'
                }), 400

            task_info = self._build_task_info(
                task_id, input_path, config, engine, start_time, status='开始处理'
            )
            payload = self._start_accepted_job(
                task_id,
                task_info,
                lambda: self._execute_crop_compare_job(
                    task_id, input_path, config, engine, infile_type
                ),
                '/crop-compare',
            )
            handed_off = True
            return payload
        except Exception as e:
            task_manager.complete_task(task_id, 'failed', str(e), error=str(e))
            return self._handle_exception(e, context='/crop-compare')
        finally:
            if not handed_off and input_path:
                cleanup_staging(os.path.dirname(input_path))

    def _execute_crop_compare_job(self, task_id, input_path, config, engine, infile_type):
        stem = semantic_stem(input_path)
        staging_dir = os.path.dirname(input_path)
        try:
            if infile_type == 'origin':
                if engine == pdf2zh or engine != pdf2zh_next:
                    config.engine = 'pdf2zh'
                    fileList = self.translate_pdf(input_path, config, task_id)
                    input_path = fileList[1]
                    if not os.path.exists(input_path):
                        raise FileNotFoundError(f'Dual file not found: {input_path}')
                else:
                    # crop-compare internally requires alternating-page TB dual.
                    config.dual_mode = 'TB'
                    config.no_dual = False
                    config.no_mono = True
                    fileList = self.translate_pdf_next(input_path, config, task_id)
                    input_path = fileList[0]
                    if not os.path.exists(input_path):
                        raise FileNotFoundError(f'Dual file not found: {input_path}')

            infile_type = self.get_filetype(input_path)
            if infile_type == 'dual-cut':
                new_path = self.get_filename_after_process(input_path, 'crop-compare', engine, stem=stem)
                self.cropper.merge_pdf(input_path, new_path)
            elif infile_type == 'dual':
                source_path = input_path
                if self.get_dual_mode(input_path, config.dual_mode) == 'LR':
                    _, source_path = self.cropper.pdf_dual_mode(input_path, 'LR', 'TB')
                new_path = self.get_filename_after_process(input_path, 'crop-compare', engine, stem=stem)
                self.cropper.crop_pdf(config, source_path, 'dual', new_path, 'crop-compare')
            else:
                raise ValueError(f'当前 PDF 类型 {infile_type} 不能执行 crop-compare。请选择原文、dual 或 dual-cut 文件。')

            final_paths = move_products_to_output([(new_path, 'crop-compare')], stem)
            if not final_paths:
                raise RuntimeError(f'Crop-compare failed: {new_path} not found')
            fileName = os.path.basename(final_paths[0])
            size = os.path.getsize(final_paths[0])
            print(f"🐲 双语对照成功(裁剪后拼接), 生成文件: {fileName}, 大小为: {size/1024.0/1024.0:.2f} MB")
            return self._complete_job_files(
                task_id, final_paths, f'成功生成 {fileName}'
            )
        finally:
            cleanup_staging(staging_dir)

    # /compare
    def compare(self):
        task_id = str(uuid.uuid4())
        start_time = datetime.now()
        input_path = None
        handed_off = False
        try:
            input_path, config = self.process_request(task_id)
            infile_type = self.get_filetype(input_path)
            engine = config.engine

            if infile_type == 'compare':
                return jsonify({
                    'status': 'error',
                    'errorType': 'InvalidPDFOperation',
                    'message': '该 PDF 已经是双语对照结果，无需再次执行 compare。请选择原文或 dual 附件。'
                }), 409
            if infile_type not in {'origin', 'dual'}:
                return jsonify({
                    'status': 'error',
                    'errorType': 'InvalidPDFOperation',
                    'message': f'当前 PDF 类型 {infile_type} 不能执行 compare。请选择原文或 dual 文件。'
                }), 400

            task_info = self._build_task_info(
                task_id, input_path, config, engine, start_time, status='开始处理'
            )
            payload = self._start_accepted_job(
                task_id,
                task_info,
                lambda: self._execute_compare_job(
                    task_id, input_path, config, engine, infile_type
                ),
                '/compare',
            )
            handed_off = True
            return payload
        except Exception as e:
            task_manager.complete_task(task_id, 'failed', str(e), error=str(e))
            return self._handle_exception(e, context='/compare')
        finally:
            if not handed_off and input_path:
                cleanup_staging(os.path.dirname(input_path))

    def _execute_compare_job(self, task_id, input_path, config, engine, infile_type):
        stem = semantic_stem(input_path)
        staging_dir = os.path.dirname(input_path)
        try:
            if infile_type == 'origin':
                if engine == pdf2zh or engine != pdf2zh_next:
                    config.engine = 'pdf2zh'
                    fileList = self.translate_pdf(input_path, config, task_id)
                    input_path = fileList[1]
                    if not os.path.exists(input_path):
                        raise FileNotFoundError(f'Dual file not found: {input_path}')
                else:
                    config.dual_mode = 'LR'
                    config.no_dual = False
                    config.no_mono = True
                    fileList = self.translate_pdf_next(input_path, config, task_id)
                    input_path = fileList[0]
                    if not os.path.exists(input_path):
                        raise FileNotFoundError(f'Dual file not found: {input_path}')

            infile_type = self.get_filetype(input_path)
            if infile_type != 'dual':
                raise ValueError(f'当前 PDF 类型 {infile_type} 不能执行 compare。请选择原文或 dual 文件。')

            new_path = self.get_filename_after_process(input_path, 'compare', engine, stem=stem)
            if self.get_dual_mode(input_path, config.dual_mode) == 'LR':
                if os.path.exists(new_path):
                    os.remove(new_path)
                shutil.copyfile(input_path, new_path)
            else:
                self.cropper.merge_pdf(input_path, new_path)

            final_paths = move_products_to_output([(new_path, 'compare')], stem)
            if not final_paths:
                raise RuntimeError(f'Compare failed: {new_path} not found')
            fileName = os.path.basename(final_paths[0])
            print(f"🐲 双语对照成功, 生成文件: {fileName}, 大小为: {os.path.getsize(final_paths[0])/1024.0/1024.0:.2f} MB")
            return self._complete_job_files(
                task_id, final_paths, f'成功生成 {fileName}'
            )
        finally:
            cleanup_staging(staging_dir)

    def get_filetype(self, path):
        name = os.path.basename(str(path))
        # 新命名 Traned_* 优先；长前缀必须先判，否则
        # Traned_single_cut_ / Traned_double_cut_ 会被短前缀误判为 mono/dual。
        traned_type = match_traned_product(name)
        if traned_type:
            return traned_type
        # 旧命名（历史文件仍在 translated 根目录，必须继续识别）。
        # Check terminal/specific suffixes before generic dual/mono markers.
        if 'crop-compare.pdf' in name:
            return 'crop-compare'
        if 'dual-cut.pdf' in name:
            return 'dual-cut'
        if 'mono-cut.pdf' in name:
            return 'mono-cut'
        if 'compare.pdf' in name:
            return 'compare'
        if name.endswith('.LR_dual.pdf') or name.endswith('.TB_dual.pdf') or 'dual.pdf' in name:
            return 'dual'
        if 'mono.pdf' in name:
            return 'mono'
        if 'cut.pdf' in name:
            return 'origin-cut'
        return 'origin'

    def get_dual_mode(self, path, fallback='TB'):
        name = os.path.basename(str(path))
        if name.endswith('.LR_dual.pdf'):
            return 'LR'
        if name.endswith('.TB_dual.pdf'):
            return 'TB'
        mode = str(fallback or 'TB').upper()
        return mode if mode in {'LR', 'TB'} else 'TB'

    def _canonicalize_pdf2zh_next_dual(self, dual_path, mode):
        if not dual_path or not os.path.exists(dual_path):
            raise FileNotFoundError(f"Dual file not found: {dual_path}")
        mode = 'LR' if str(mode).upper() == 'LR' else 'TB'
        path = str(dual_path)
        if path.endswith('.LR_dual.pdf') or path.endswith('.TB_dual.pdf'):
            current = self.get_dual_mode(path)
            if current == mode:
                return path
            lr_path, tb_path = self.cropper.pdf_dual_mode(path, current, mode)
            return lr_path if mode == 'LR' else tb_path
        if path.endswith('.dual.pdf'):
            target = path[:-len('.dual.pdf')] + f'.{mode}_dual.pdf'
        else:
            target = path[:-4] + f'.{mode}_dual.pdf' if path.endswith('.pdf') else path + f'.{mode}_dual.pdf'
        if os.path.exists(target):
            os.remove(target)
        os.replace(path, target)
        return target

    def get_filetype_after_crop(self, path):
        filetype = self.get_filetype(path)
        print(f"🔍 [Zotero PDF2zh Server] 获取文件类型: {filetype} from {path}")
        if filetype == 'origin':
            return 'origin-cut'
        if filetype == 'mono':
            return 'mono-cut'
        if filetype == 'dual':
            return 'dual-cut'
        return 'unknown'

    def get_filetype_after_cropCompare(self, path):
        filetype = self.get_filetype(path)
        if filetype in {'origin', 'dual', 'dual-cut'}:
            return 'crop-compare'
        return 'unknown'

    def get_filetype_after_compare(self, path):
        filetype = self.get_filetype(path)
        if filetype in {'origin', 'dual'}:
            return 'compare'
        return 'unknown'

    def get_filename_after_process(self, inpath, outtype, engine=None, stem=None):
        """返回 staging 内的中间产物路径（最终由 move_products_to_output 归档）。

        旧实现按 ``{intype}.pdf`` 做字符串替换，遇到 ``Traned_*`` 新命名时
        替换不生效、会返回输入本身；现在统一用语义 stem 生成
        ``<stem>.<outtype>.pdf``（原文裁剪沿用 ``.cut.pdf``），因此新命名与
        旧命名输入都能得到正确的中间产物名。

        Args:
            inpath: 中间输入文件（位于本任务的 staging 子目录）。
            outtype: 产物类型，如 ``mono-cut`` / ``compare``。
            engine: 保留参数以兼容调用方（中间产物命名不再区分引擎）。
            stem: 语义 stem；缺省时从 ``inpath`` 推导（剥 Traned_* 前缀）。
        """
        inpath = str(inpath)
        if stem is None:
            stem = semantic_stem(inpath)
        filename = f"{stem}.cut.pdf" if outtype == 'origin-cut' else f"{stem}.{outtype}.pdf"
        return os.path.join(os.path.dirname(inpath), filename)

    def translate_pdf(self, input_path, config, task_id=None):
        # TODO: 如果翻译失败了, 自动执行跳过字体子集化, 并且显示生成的文件的大小
        config.update_config_file(config_path[pdf2zh])
        if config.targetLang == 'zh-CN': # TOFIX, pdf2zh 1.x converter没有通过
            config.targetLang = 'zh'
        if config.sourceLang == 'zh-CN': # TOFIX, pdf2zh 1.x converter没有通过
            config.sourceLang = 'zh'
        # 内核产物写入本任务的 staging 子目录，避免落到 translated 根
        output_dir = os.path.dirname(input_path)
        cmd = [
            pdf2zh,
            input_path,
            '--t', str(config.thread_num),
            '--output', str(output_dir),
            '--service', str(config.service),
            '--lang-in', str(config.sourceLang),
            '--lang-out', str(config.targetLang),
            '--config', str(config_path[pdf2zh]), # 使用默认的config path路径
        ]

        if config.skip_last_pages and config.skip_last_pages > 0:
            end = len(PdfReader(input_path).pages) - config.skip_last_pages
            cmd.append('-p '+str(1)+'-'+str(end))
        if config.skip_font_subsets:
            cmd.append('--skip-subset-fonts')
        if config.babeldoc:
            print("🔍 [Zotero PDF2zh Server] 目前不推荐使用pdf2zh 1.x + babeldoc, 如有需要，请直接使用pdf2zh_next")
            cmd.append('--babeldoc')
        try:
            # 使用 execute_with_progress 替代原来的 execute_in_env / subprocess.run
            # 实时解析子进程输出中的进度信息并更新 task_manager
            execute_with_progress(cmd, task_id, args, self.env_manager if args.enable_venv else None)
        except subprocess.CalledProcessError as e:
            print(f"⚠️ 翻译失败, 错误信息: {e}, 尝试跳过字体子集化, 重新渲染\n")
            cmd.append('--skip-subset-fonts')
            execute_with_progress(cmd, task_id, args, self.env_manager if args.enable_venv else None)
        fileName = os.path.basename(input_path).replace('.pdf', '')
        if config.babeldoc:
            output_path_mono = os.path.join(output_dir, f"{fileName}.{config.targetLang}.mono.pdf")
            output_path_dual = os.path.join(output_dir, f"{fileName}.{config.targetLang}.dual.pdf")
        else:
            output_path_mono = os.path.join(output_dir, f"{fileName}-mono.pdf")
            output_path_dual = os.path.join(output_dir, f"{fileName}-dual.pdf")
        output_files = [output_path_mono, output_path_dual]
        for f in output_files: # 显示生成
            if not os.path.exists(f):
                print(f"⚠️ 未找到期望生成的文件: {f}")
                continue
            size = os.path.getsize(f)
            print(f"🐲 pdf2zh 翻译成功, 生成文件: {f}, 大小为: {size/1024.0/1024.0:.2f} MB")
        return output_files

    def translate_pdf_next(self, input_path, config, task_id=None):
        if config.service in pdf2zh_next_service_aliases:
            config.service = pdf2zh_next_service_aliases[config.service]
        config.update_config_file(config_path[pdf2zh_next])

        # 内核产物写入本任务的 staging 子目录，避免落到 translated 根
        output_dir = os.path.dirname(input_path)
        cmd = [
            pdf2zh_next,
            input_path,
            '--' + config.service,
            '--qps', str(config.qps),
            '--output', str(output_dir),
            '--lang-in', str(config.sourceLang),
            '--lang-out', str(config.targetLang),
            '--config-file', str(config_path[pdf2zh_next]), # 使用默认的config path路径
        ]
        # TODO: 增加术语表的地址
        if config.no_watermark:
            cmd.extend(['--watermark-output-mode', 'no_watermark'])
        else:
            cmd.extend(['--watermark-output-mode', 'watermarked'])
        if config.skip_last_pages and config.skip_last_pages > 0:
            end = len(PdfReader(input_path).pages) - config.skip_last_pages
            cmd.extend(['--pages', f'{1}-{end}'])
        if config.no_dual:
            cmd.append('--no-dual')
        if config.no_mono:
            cmd.append('--no-mono')
        if config.trans_first:
            cmd.append('--dual-translate-first')
        if config.skip_clean:
            cmd.append('--skip-clean')
        if config.disable_rich_text_translate:
            cmd.append('--disable-rich-text-translate')
        if config.enhance_compatibility:
            cmd.append('--enhance-compatibility')
        if config.save_auto_extracted_glossary:
            cmd.append('--save-auto-extracted-glossary')
        if config.disable_glossary:
            cmd.append('--no-auto-extract-glossary')
        if config.dual_mode == 'TB': # TB or LR, LR是defualt的
            cmd.append('--use-alternating-pages-dual')
        if config.translate_table_text:
            cmd.append('--translate-table-text')
        if config.ocr:
            cmd.append('--ocr-workaround')
        if config.auto_ocr:
            cmd.append('--auto-enable-ocr-workaround')
        if config.font_family and config.font_family in ['serif', 'sans-serif', 'script']:
            cmd.extend(['--primary-font-family', config.font_family])
        if config.pool_size and config.pool_size > 1:
            cmd.extend(['--pool-max-worker', str(config.pool_size)])

        fileName = os.path.basename(input_path).replace('.pdf', '')
        no_watermark_mono = os.path.join(output_dir, f"{fileName}.no_watermark.{config.targetLang}.mono.pdf")
        no_watermark_dual = os.path.join(output_dir, f"{fileName}.no_watermark.{config.targetLang}.dual.pdf")
        watermark_mono = os.path.join(output_dir, f"{fileName}.{config.targetLang}.mono.pdf")
        watermark_dual = os.path.join(output_dir, f"{fileName}.{config.targetLang}.dual.pdf")

        output_path = []
        if config.no_watermark: # 无水印
            if not config.no_mono:
                output_path.append(no_watermark_mono)
            if not config.no_dual:
                output_path.append(no_watermark_dual)
        else: # 有水印
            if not config.no_mono:
                output_path.append(watermark_mono)
            if not config.no_dual:
                output_path.append(watermark_dual)

        if args.enable_winexe and os.path.exists(args.winexe_path):
            cmd = [f"{args.winexe_path}"] + cmd[1:]  # Windows可执行文件
            # 将所有是路径的字段, 改为os.path.normpath
            cmd = [os.path.normpath(arg) if os.path.isfile(arg) or os.path.isdir(arg) else arg for arg in cmd]
            # 设置工作目录为 exe 所在目录，确保相对路径解析正确
            exe_dir = os.path.dirname(args.winexe_path)

            # 打印开关状态
            print(f"🔧 [winexe] winexe_attach_console={args.winexe_attach_console}")

            if args.winexe_attach_console:

                # 附着父控制台模式
                print("🚀 [winexe] mode=attach-console")
                print(f"📁 [winexe] cwd={exe_dir}")

                # 隐藏敏感信息后的命令显示
                safe_cmd = []
                for i, arg in enumerate(cmd):
                    if i > 0 and any(sensitive in cmd[i-1].lower() for sensitive in ['key', 'token', 'secret', 'password']):
                        safe_cmd.append('***')
                    else:
                        safe_cmd.append(arg)
                print(f"⚡ [winexe] cmd={' '.join(safe_cmd)}")

                # Do not launch a separate ``--help`` process just to probe
                # console visibility.  Some standalone pdf2zh executables do
                # substantial initialization even for help output.  The real
                # translation process below is the only process needed here;
                # DeepSeek capability validation remains handled separately.
                # 执行主命令 - 附着父控制台
                print("🔍 [winexe] 开始执行（预期在当前终端显示实时日志）...")
                process = subprocess.Popen(
                    cmd,
                    shell=False,
                    cwd=exe_dir,
                    stderr=subprocess.PIPE,
                    text=True,
                    bufsize=1,
                )

                stderr_lines = []
                if process.stderr:
                    for line in process.stderr:
                        stderr_lines.append(line)
                        sys.stderr.write(line)
                        sys.stderr.flush()
                    process.stderr.close()

                return_code = process.wait()
                if return_code != 0:
                    stderr_text = ''.join(stderr_lines)
                    value_error = self._extract_value_error(stderr_text)
                    if value_error:
                        raise ValueError(value_error)
                    print(f"❌ pdf2zh.exe 执行失败，退出码: {return_code}")
                    print("   操作失败，请查看详细日志。")
                    raise RuntimeError(f"pdf2zh.exe 执行失败，退出码: {return_code}")

            else:
                # 回退模式：静默模式（旧行为）
                print("🔇 [winexe] mode=silent")
                r = subprocess.run(
                    cmd,
                    shell=False,
                    cwd=exe_dir,
                    creationflags=CREATE_NO_WINDOW,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8"
                )
                if r.returncode != 0:
                    value_error = self._extract_value_error(r.stderr or '')
                    if value_error:
                        raise ValueError(value_error)
                    raise RuntimeError(f"pdf2zh.exe 退出码 {r.returncode}\nstdout:\n{r.stdout}\nstderr:\n{r.stderr}")
        elif args.enable_venv:
            # 使用 execute_with_progress 替代原来的 execute_in_env
            # 实时解析子进程输出中的进度信息并更新 task_manager
            execute_with_progress(cmd, task_id, args, self.env_manager)
        else:
            execute_with_progress(cmd, task_id, args, None)
        existing = [p for p in output_path if os.path.exists(p)]

        for f in existing:
            size = os.path.getsize(f)
            print(f"🐲 pdf2zh_next 翻译成功, 生成文件: {f}, 大小为: {size/1024.0/1024.0:.2f} MB")

        if not existing:
            raise RuntimeError("操作失败，请查看详细日志。")

        return existing

    def run(self, host, port, debug=False):
        print(f"🌐 Server将启动在: http://{host}:{port}")
        print(f"📊 翻译进度监控页面: http://localhost:{port}/")
        print(f"💡 健康检查端点: http://localhost:{port}/health")
        self.app.run(
            host=host,
            port=port,
            debug=debug,
            threaded=True,
            request_handler=_QuietAccessHandler,
        )

def prepare_path():
    os.makedirs(output_folder, exist_ok=True)
    # 运行期配置目录: <程序根>/config/pdf-cache（cache_dir 已按 PDF2ZH_HOME 推导）
    os.makedirs(runtime_config_folder, exist_ok=True)
    # Windows 下把 config 与 pdf-cache 两级目录设为隐藏，失败不影响启动
    if os.name == 'nt':
        ensure_hidden(config_folder)
        ensure_hidden(runtime_config_folder)
    # 旧版运行期配置直接放在 server/config；仅复制到新位置，保留旧文件
    migrated = migrate_legacy_configs(config_folder, runtime_config_folder)
    if migrated:
        names = ', '.join(os.path.basename(path) for path in migrated)
        print(f"✅ [配置迁移] 旧配置已复制到 {runtime_config_folder}: {names}")
    # Never overwrite an existing user config with the .example template.
    # Migration only adds missing defaults; user/custom values always win.
    prepare_config_files(config_path)

# ================================================================================
# ######################### 主程序入口 ############################
# ================================================================================

def str2bool(v):
    if isinstance(v, bool):
        return v
    if v.lower() in ('yes', 'true', 't', '1', 'y'):
        return True
    elif v.lower() in ('no', 'false', 'f', '0', 'n'):
        return False
    else:
        raise argparse.ArgumentTypeError('Boolean value expected.')

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', type=str, default='127.0.0.1', help='Server bind host; use 0.0.0.0 only when remote access is intentionally required')
    parser.add_argument('--port', type=int, default=PORT, help='Port to run the server on')

    parser.add_argument('--enable_venv', type=str2bool, default=enable_venv, help='脚本自动开启虚拟环境')
    parser.add_argument('--env_tool', choices=['auto', 'uv', 'conda'], default=default_env_tool, help='环境管理工具；auto 会沿用已有 uv/conda，新环境优先 uv')
    parser.add_argument('--check_update', type=str2bool, default=True, help='启动时检查更新')
    parser.add_argument('--update_source', type=str, default='gitee', help='优先更新源 gitee 或 github；失败时自动尝试另一个源')
    parser.add_argument('--debug', type=str2bool, default=False, help='Enable debug mode')
    parser.add_argument('--enable_winexe', type=str2bool, default=False, help='使用pdf2zh_next Windows可执行文件运行脚本, 仅限Windows系统')
    parser.add_argument('--enable_mirror', type=str2bool, default=True, help='启用下载镜像加速, 仅限中国大陆用户')
    parser.add_argument('--mirror_source', type=str, default='https://mirrors.ustc.edu.cn/pypi/simple', help='自定义您的PyPI镜像源, 仅限中国大陆用户')
    parser.add_argument('--winexe_path', type=str, default='./pdf2zh-v2.6.3-BabelDOC-v0.5.7-win64/pdf2zh/pdf2zh.exe', help='Windows可执行文件的路径')
    parser.add_argument('--winexe_attach_console', type=str2bool, default=True, help='Winexe模式是否尝试附着父控制台显示实时日志 (默认True)')
    parser.add_argument('--skip_install', type=str2bool, default=False, help='跳过虚拟环境中的安装')
    args = parser.parse_args()
    # 2. 打印提示信息
    print("\n===== 💡提示💡 =====")
    print("如果您遇到问题......")
    print("1️⃣ 请阅读本项目的【github主页】, 这里有最准确的信息")
    print("    · 🤖 github: https://github.com/BluesilveEmperor/PDFTranslate-Zotero")
    print("    · 🤖 如果国内无法访问github, 请移步: gitee: https://github.com/BluesilveEmperor/PDFTranslate-Zotero\n")

    print("2️⃣ 加入zotero-pdf2zh插件QQ群: 请在 GitHub / Gitee 仓库主页查看最新群号和入群口令")
    print("    · 【提问前】您需要先确保已经阅读过本项目主页的教程以及常见问题汇总")
    print("    · 【提问时】您必须将本终端输出的所有信息复制到txt文件中, 并截图您的zotero插件设置, 一并发送到群里, 否则您将不会得到回复, 感谢配合!\n")

    print("\n==== 🌍翻译期间请勿关闭此窗口🌍 =====\n")

    # 3. 打印启动参数
    print("🚀 启动参数:", args, "\n")
    print("🏠 当前版本: ", __version__)
    print("🏠 当前路径: ", root_path, "\n")

    # 4. 环境检查（端口、目录权限、Python版本、虚拟环境）
    print("🔍 开始环境检查...")
    all_checks_passed = True

    # 4.1 端口检查
    print("\n--- 网络端口检查 ---")
    port = args.port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        print(f"🔍 检查端口 {port} 是否被占用...")
        if s.connect_ex(('localhost', port)) == 0:
            print(f"❌ 端口 {port} 已被占用！")
            print("\n💡 解决方案:")
            print("   1. 选择其他端口启动: python server.py --port XXXX")
            print("   2. 或在Zotero插件设置中修改Server IP端口号")
            print(f"   3. 或停止占用端口 {port} 的其他程序")
            all_checks_passed = False
        else:
            print(f"✅ 端口 {port} 可用")

    # 4.2 目录权限检查
    print("\n--- 目录权限检查 ---")
    required_dirs = [
        ('translated', '翻译输出目录', output_folder),
        ('config', '配置模板目录', config_folder),
        (os.path.join('config', 'pdf-cache'), '运行期配置目录', runtime_config_folder),
    ]

    for dir_name, description, dir_path in required_dirs:
        if not os.path.exists(dir_path):
            print(f"⚠️  {description} ({dir_name}) 不存在，尝试创建...")
            try:
                os.makedirs(dir_path, exist_ok=True)
                print(f"✅ {description} 创建成功: {dir_path}")
            except Exception as e:
                print(f"❌ 无法创建 {description}: {e}")
                print(f"\n💡 解决方案:")
                print(f"   1. 手动创建 {dir_name} 文件夹")
                print(f"   2. 检查当前用户是否有创建目录的权限")
                print(f"   3. 尝试以管理员身份运行（Windows: 右键'以管理员身份运行'）")
                all_checks_passed = False
        else:
            # 检查写入权限
            if not os.access(dir_path, os.W_OK):
                print(f"❌ {description} ({dir_name}) 没有写入权限！")
                print(f"\n💡 解决方案:")
                print(f"   1. 检查 {dir_name} 文件夹的权限设置")
                print(f"   2. 在Windows中: 右键文件夹 -> 属性 -> 安全 -> 编辑权限")
                print(f"   3. 在Linux/Mac中: chmod 755 {dir_path}")
                all_checks_passed = False
            else:
                print(f"✅ {description} ({dir_name}) 权限正常")

    # 4.3 Python版本检查
    print("\n--- Python环境检查 ---")
    print(f"🐍 Python版本: {sys.version}")
    major, minor = sys.version_info[:2]
    if major < 3 or (major == 3 and minor < 8):
        print(f"❌ Python版本过低！需要 Python 3.8 或更高版本")
        print(f"💡 解决方案:")
        print(f"   1. 安装 Python 3.8 或更高版本")
        print(f"   2. 从 python.org 下载最新版 Python")
        all_checks_passed = False
    else:
        print(f"✅ Python版本符合要求")

    # 4.4 虚拟环境检查
    if args.enable_venv:
        print("\n--- 虚拟环境检查 ---")
        print(f"🔧 环境管理模式: {args.env_tool}")
        if args.env_tool == 'auto':
            print("💡 auto: 优先沿用已有 uv/conda；没有已有环境时优先创建 uv。")

        found = []
        for engine_name in (pdf2zh, pdf2zh_next):
            existing = find_existing_environment(engine_name, args.env_tool)
            if existing:
                tool, env_dir, python_path = existing
                found.append(engine_name)
                print(f"✅ {engine_name}: {tool} -> {env_dir}")
                try:
                    versions = read_versions(python_path, engine_name)
                    printed = format_versions(versions)
                    if printed:
                        print(f"   📦 {printed}")
                    if engine_name == pdf2zh_next and not pdf2zh_next_meets_minimum(
                        versions.get("pdf2zh-next")
                    ):
                        print(
                            "   ⚠️ pdf2zh_next 低于 2.9.0，DeepSeek V4 思考控制可能不可用。"
                        )
                except Exception as exc:
                    print(f"   ⚠️ 无法读取包版本: {exc}")
            else:
                print(f"ℹ️ {engine_name}: 暂无托管环境，首次使用时将自动创建。")
        if not found:
            print("💡 尚未创建翻译环境；Server 本身可以先正常启动。")

    # 检查总结
    print("\n" + "="*60)
    if all_checks_passed:
        print("✅ 所有检查通过！Server准备启动...")
    else:
        print("❌ 部分检查未通过，可能影响Server正常运行")
        print("\n⚠️  您可以选择:")
        print("   1. 根据上述提示修复问题后重新启动")
        print("   2. 忽略警告继续运行（可能遇到错误）")

        user_input = input("\n是否继续启动？(y/n): ").strip().lower()
        if user_input != 'y':
            print("👋 已取消启动，请修复问题后重试")
            sys.exit(0)

    print("="*60 + "\n")
    print("💡 请保持此窗口开启，翻译期间请勿关闭\n")

    # 5. 拉取仓库通知（失败则跳过，不影响启动）
    try:
        fetch_and_show_notices(__version__, args.update_source)
    except Exception:
        pass

    # 6. 启动时自动检查 Server 源码更新
    if args.check_update:
        print("🔍 开始检查 Server 更新...")
        update_info = check_for_updates(__version__, args.update_source)
        if update_info:
            local_v, remote_v = update_info
            print(f"🎉 发现新版本！当前版本: {local_v}, 最新版本: {remote_v}")
            try:
                answer = input("是否要立即更新? (y/n): ").lower()
            except (EOFError, KeyboardInterrupt):
                answer = 'n'
                print("\n无法获取用户输入，已自动取消更新。")

            if answer in ['y', 'yes']:
                perform_update_optimized(root_path, __version__, expected_version=remote_v, update_source=args.update_source)
            else:
                print("👌 已取消 Server 源码更新。")

    # 7. 配置迁移 + 正常启动。VirtualEnvManager 会在这里对已有用户
    #    每个 Server 版本最多询问一次是否安全更新翻译环境。
    prepare_path()
    translator = PDFTranslator(args)
    translator.run(args.host, args.port, debug=args.debug)

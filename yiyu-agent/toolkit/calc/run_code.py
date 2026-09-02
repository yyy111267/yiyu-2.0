"""
run_code 工具 —— 断网计算沙箱。

定位：标准指标（calc.metric 覆盖不到的）之外的非标计算
（SOTP 分部加总、敏感性表、自定义单位经济模型等），LLM 在断网沙箱里写 Python 现算。
这是"固定公式"与"LLM 现算"分界线的右半侧。

四条铁律（缺一条方案就塌）：
  1. 断网是**运行时强制**：子进程内替换 socket 模块 + 禁 subprocess/ctypes/os.system
     + 禁文件写。不是靠 prompt 约定，是沙箱物理隔离。
  2. 输入 = **取数引擎的数据包**（DATA，只读 JSON，按 symbol 从统一取数缓存现组装）。
     LLM 只能按 fields 白名单选字段，不能往代码里敲数字、不能自己造值。
  3. 可复现：每次执行存档三件套（代码, 数据包 hash, 输出）到 var/run_code_logs/。
     否则 LLM 现算脚本与"心算"在可复现性上是同罪。
  4. 可 import formulas_core / formulas_<group>：有标准口径的指标优先调库，
     code 留空时返回可信函数库索引（函数名 + 一行口径），供 LLM 挑选。

注意：这是"计算沙箱"，不是"安全沙箱"——隔离目标是防取数失控、防心算，
不是防恶意代码。不要把不可信的外部代码喂进来。
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import logging
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from toolkit.base import ReadOnlyTool, ToolSchema
from toolkit.calc.metric_base import _build_data_pack, _map_fields
from toolkit.market.research_data import get_research_bundle

logger = logging.getLogger(__name__)

# ── 路径约定 ────────────────────────────────────────────────
_ROOT = Path(__file__).resolve().parents[2]                     # yiyu-agent/
_BUS_ROUTER_DIR = _ROOT / "bus_router"
_LOG_DIR = _ROOT / "var" / "run_code_logs"

# ── 沙箱加固代码（子进程开头执行，物理断网）───────────────────
# 用 r-string + format 注入 bus_router 路径；数据包路径经环境变量传入。
_SANDBOX_PREAMBLE = r'''# -*- coding: utf-8 -*-
import os as _os, sys as _sys, builtins as _builtins

def _deny(*_a, **_k):
    raise RuntimeError("[run_code] 沙箱禁令：断网只读环境，禁止网络/子进程/文件写/危险模块")

# ── 1) 断网：替换 socket 模块（一切网络调用抛错）──
import socket as _sock
class _BlockedSocket:
    def __getattr__(self, name):
        raise RuntimeError("[run_code] 网络已禁用（socket.%s），沙箱不能取数" % name)
    def __call__(self, *a, **k):
        raise RuntimeError("[run_code] 网络已禁用，沙箱不能取数")
_sys.modules["socket"] = _BlockedSocket()

for _m in ("urllib.request", "urllib", "http.client", "http", "requests",
           "aiohttp", "httpx", "ftplib", "imaplib", "smtplib", "telnetlib",
           "poplib", "xmlrpc.client", "urllib3"):
    _sys.modules[_m] = None

# ── 2) 禁子进程 / 系统调用 / 危险模块 ──
import subprocess as _sp
for _f in ("Popen", "run", "call", "check_call", "check_output",
           "getoutput", "getstatusoutput"):
    setattr(_sp, _f, _deny)
_sys.modules["subprocess"] = None

_os.system = _deny; _os.popen = _deny; _os.spawnl = _deny; _os.spawnv = _deny
_os.fork = _deny; _os.posix_spawn = _deny; _os.execl = _deny; _os.execv = _deny
_os.remove = _deny; _os.unlink = _deny; _os.rmdir = _deny
_os.rename = _deny; _os.replace = _deny

_sys.modules["ctypes"] = None
_sys.modules["pty"] = None

# ── 3) 文件系统：只读（仅允许读数据包文件）──
_orig_open = _builtins.open
def _safe_open(file, mode="r", *a, **k):
    if any(c in str(mode) for c in "wax+"):
        raise RuntimeError("[run_code] 文件写已禁用")
    return _orig_open(file, mode, *a, **k)
_builtins.open = _safe_open

import shutil as _sh
_sh.rmtree = _deny; _sh.move = _deny; _sh.copy = _deny; _sh.copyfile = _deny
import pathlib as _pl
for _n in ("unlink", "rmdir", "write_text", "write_bytes", "touch", "mkdir",
           "symlink_to", "hardlink_to"):
    setattr(_pl.Path, _n, lambda *a, **k: _deny())

# ── 4) 注入可信函数库路径：可 import formulas_core / formulas_<group> ──
_sys.path.insert(0, __BUS_ROUTER_DIR__)

# ── 5) 数据包：引擎写入的只读 JSON。LLM 只能选字段，不能造数字 ──
import json as _json
_DATA_FILE = _os.environ.get("YIYU_DATA_FILE", "")
if _DATA_FILE:
    with _orig_open(_DATA_FILE, "r", encoding="utf-8") as _fh:
        DATA = _json.load(_fh)
else:
    DATA = {}
'''

_SANDBOX_PREAMBLE = _SANDBOX_PREAMBLE.replace(
    "__BUS_ROUTER_DIR__", json.dumps(str(_BUS_ROUTER_DIR))
)


class RunCodeTool(ReadOnlyTool):
    """断网计算沙箱工具。"""

    schema = ToolSchema(
        name="calc.run_code",
        description=(
            "断网计算沙箱：执行 LLM 自写的 Python 脚本，计算标准指标之外的非标指标"
            "（SOTP 分部加总、敏感性表、自定义单位经济模型等），或补算 calc.metric 标 NC 的指标"
            "（用数据包里的相邻字段做近似，标 DEGRADED）。\n"
            "铁律：\n"
            "① 断网只读——禁止网络/子进程/文件写，运行时强制，不是约定；\n"
            "② 数值只能来自数据包 DATA（按 fields 白名单选字段，以 DATA['字段名'] 访问）。"
            "数据包按 symbol 从 market.get_bundle 的统一取数缓存现组装，须先取数；"
            "或经 web_data 显式传入的 web 检索数字（DATA['_web']['字段名']['value']）；"
            "禁止在代码里写死业务数字（等于心算）；\n"
            "③ 有标准口径的指标（ROIC/TTM差分/正常化盈利/应计项/三情景估值/近似推导库 "
            "ebit_approx/ebitda_approx/ocf_approx 等）"
            "必须 from formulas_core import ... 或 from formulas_<组> import ...，不许自己手写公式；\n"
            "④ 可信函数返回 MetricResult（不是裸数字）：读 .value 取数值、.status 判四态"
            "（OK 可解读 / NOT_APPLICABLE 不适用 / NOT_COMPUTABLE 数据缺失 / DEGRADED 口径近似），"
            ".reason 看原因；用近似推导或 web 数据算出的指标必须返回 DEGRADED（如 "
            "DEGRADED(值, 'EBIT 用营业利润+财务费用近似')），并注明 provenance，禁止伪装 OK；\n"
            "⑤ 每次执行自动存档(代码, 数据包hash+web_data hash, 输出)，可复现。\n"
            "code 留空时返回可信函数库索引（函数名+一行口径），供挑选现成函数。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "证券代码或公司名（如 600519.SH / 贵州茅台）。"
                                   "数据包按它从 market.get_bundle 的统一取数缓存组装，须先取数。",
                },
                "code": {
                    "type": "string",
                    "description": "要执行的 Python 代码。数据包字段用 DATA['字段名'] 访问"
                                   "（取数引擎预取的原始值/序列，时间升序）；"
                                   "web 检索数字用 DATA['_web']['字段名']['value'] 访问。",
                },
                "fields": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "本次计算要用的数据字段白名单（从引擎数据包中选，不能自己造字段）",
                },
                "web_data": {
                    "type": "object",
                    "description": (
                        "（可选）用 web.search/web.fetch 检索到的可信数字。"
                        "格式：{\"字段名\": {\"value\": 数值, \"url\": \"来源链接\", \"note\": \"检索说明\"}}，"
                        "沙箱内经 DATA['_web']['字段名']['value'] 访问。"
                        "web 来源数据算出的指标必须标 DEGRADED 并在结论中写明来源 URL。"
                    ),
                },
                "group": {
                    "type": "string",
                    "description": "商业模式分组（G1a/G1b/G2a...），决定注入哪个 formulas_<group> 函数库。默认 core",
                    "default": "core",
                },
            },
            "required": ["symbol", "code", "fields"],
        },
        read_only=True,
        max_chars=8000,
        timeout_seconds=45,
    )

    # ── 空调用：返回可信函数库索引 ──────────────────────────
    def _formula_index(self, group: str) -> dict:
        module_name = "formulas_core" if group in ("", "core") else f"formulas_{group}"
        path = _BUS_ROUTER_DIR / f"{module_name}.py"
        if not path.exists():
            return {
                "success": False,
                "error": f"公式库 {module_name}.py 不存在",
                "available_libraries": sorted(
                    p.stem for p in _BUS_ROUTER_DIR.glob("formulas_*.py")
                ),
            }
        # 组模块内部会 `from formulas_core import ...`，主进程加载时临时注入 bus_router 到 sys.path
        added = False
        if str(_BUS_ROUTER_DIR) not in sys.path:
            sys.path.insert(0, str(_BUS_ROUTER_DIR))
            added = True
        try:
            spec = importlib.util.spec_from_file_location(module_name, path)
            if spec is None or spec.loader is None:
                return {
                    "success": False,
                    "error": f"公式库 {module_name}.py 无法加载（spec 无效）",
                    "available_libraries": sorted(
                        p.stem for p in _BUS_ROUTER_DIR.glob("formulas_*.py")
                    ),
                }
            mod = importlib.util.module_from_spec(spec)
            # ★ Python 3.13+ 的 dataclasses 会查 sys.modules[cls.__module__].__dict__，
            #   模块不注册进 sys.modules 会在 @dataclass 处抛 AttributeError。
            #   先注册再执行，执行完再清理，避免污染主进程导入缓存。
            sys.modules[module_name] = mod
            try:
                spec.loader.exec_module(mod)  # type: ignore[union-attr]
            finally:
                sys.modules.pop(module_name, None)
            return {
                "success": True,
                "library": module_name,
                "formula_count": len(getattr(mod, "REGISTRY", {})),
                "formulas": mod.formula_index(),
            }
        except Exception as e:  # noqa: BLE001 - 索引生成失败不阻塞执行
            logger.warning("formula 索引加载失败: %s", e)
            return {"success": False, "error": f"公式库加载失败: {e}"}
        finally:
            if added:
                try:
                    sys.path.remove(str(_BUS_ROUTER_DIR))
                except ValueError:
                    pass

    # ── 组装数据包 + 白名单过滤 ─────────────────────────────
    def _load_data(self, symbol: str, group: str,
                   fields: list[str]) -> tuple[dict | None, str | None]:
        # 数据包按 symbol 从统一取数缓存现组装（不再落盘）：既保证是本轮该标的的
        # 数据，也省掉"必须先调某个工具写文件"的隐式顺序依赖。
        stored = get_research_bundle(symbol)
        if stored is None:
            return None, (
                f"统一行情数据包不存在：{symbol}（请先调用 market.get_bundle 取数。"
                "run_code 的输入只能是引擎的数据包，不能自己造数字）"
            )
        _, bundle = stored
        data = _build_data_pack(symbol, group, bundle, _map_fields(bundle))
        missing = [f for f in (fields or []) if f not in data]
        if missing:
            return None, (
                f"以下字段不在数据包中（LLM 只能选数据包里的字段，不能自己造）：{missing}"
            )
        subset = {f: data[f] for f in (fields or [])}
        return subset, None

    # ── 执行 ────────────────────────────────────────────────
    async def execute(self, code: str, fields: list[str], symbol: str = "",
                      group: str = "core", web_data: Any = None, **kwargs: Any) -> dict:
        code = (code or "").strip()
        symbol = str(symbol or "").strip()
        if not symbol:
            return {"success": False, "error": "symbol 为空（数据包按 symbol 从统一取数缓存组装）"}
        # 空 code → 返回可信函数库索引（让 LLM 先看库里有什么）
        if not code:
            return self._formula_index(group)

        # ① 数据包（输入只能是引擎的数据包 + 显式 web_data 补数通道）
        data, err = self._load_data(symbol, group, fields or [])
        if err is not None:
            return {"success": False, "error": err}
        # ② 注入 web_data：LLM 用 web.search/fetch 抓到的可信数字（带来源 URL）。
        #    命名空间 _web 与引擎数据包隔离；沙箱内仍断网，web_data 是外部传入的只读数据。
        #    web 来源数据算出的指标必须标 DEGRADED（协议在 schema description 里强制）。
        if web_data:
            if not isinstance(web_data, dict):
                return {"success": False, "error": "web_data 须为 {字段名: {value, url, note}} 对象"}
            normalized: dict[str, dict] = {}
            for k, v in web_data.items():
                if not isinstance(v, dict) or "value" not in v:
                    return {"success": False,
                            "error": f"web_data['{k}'] 须为 {{value, url, note}} 结构（value 必填）"}
                normalized[str(k)] = {
                    "value": v.get("value"),
                    "url": str(v.get("url") or ""),
                    "note": str(v.get("note") or ""),
                }
            data["_web"] = normalized

        # ③ 写临时数据文件（子进程通过环境变量读取，避免 argv 超长）
        fd, tmp_path = tempfile.mkstemp(prefix="run_code_data_", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False)
            script = _SANDBOX_PREAMBLE + "\n\n# ---- 用户代码 ----\n" + code

            env = {**os.environ, "YIYU_DATA_FILE": tmp_path, "PYTHONIOENCODING": "utf-8"}
            try:
                proc = await asyncio.create_subprocess_exec(
                    sys.executable, "-c", script,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                )
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(), timeout=max(1, self.schema.timeout_seconds - 5)
                )
            except asyncio.TimeoutError:
                try:
                    proc.kill()  # type: ignore[possibly-undefined]
                except Exception:  # noqa: BLE001
                    pass
                return {"success": False, "error": f"脚本执行超时（{self.schema.timeout_seconds}s）"}
        finally:
            try:
                os.remove(tmp_path)
            except OSError:
                pass

        # ③ 存档三件套：(代码, 数据包 hash, 输出)
        data_hash = hashlib.sha256(
            json.dumps(data, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        log_entry = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "group": group,
            "code": code,
            "fields": sorted(fields or []),
            "web_fields": sorted(web_data) if isinstance(web_data, dict) else [],
            "data_package_hash": data_hash,
            "exit_code": proc.returncode,
            "stdout": stdout_b.decode("utf-8", errors="replace"),
            "stderr": stderr_b.decode("utf-8", errors="replace"),
        }
        log_file = self._archive(log_entry)

        out_text = stdout_b.decode("utf-8", errors="replace").strip()
        err_text = stderr_b.decode("utf-8", errors="replace").strip()
        if proc.returncode != 0:
            return {
                "success": False,
                "error": err_text[-2000:] or f"退出码 {proc.returncode}",
                "stdout": out_text[-4000:],
                "data_package_hash": data_hash,
                "log_file": log_file,
            }
        return {
            "success": True,
            "output": out_text[-4000:] or "(无 stdout 输出)",
            "data_package_hash": data_hash,
            "log_file": log_file,
        }

    def _archive(self, entry: dict) -> str:
        """存档三件套，返回日志文件路径（供分析报告引用）。"""
        try:
            _LOG_DIR.mkdir(parents=True, exist_ok=True)
            fname = f"{time.strftime('%Y%m%d_%H%M%S')}_{entry['data_package_hash'][:8]}.json"
            path = _LOG_DIR / fname
            path.write_text(json.dumps(entry, ensure_ascii=False, indent=2), encoding="utf-8")
            return str(path)
        except Exception as e:  # noqa: BLE001 - 存档失败不阻塞执行
            logger.warning("run_code 存档失败: %s", e)
            return f"<存档失败: {e}>"


RUN_CODE_TOOLS: list[ReadOnlyTool] = [RunCodeTool()]

#!/usr/bin/env python
"""用真实 AstrBot 包做导入检查（不启动机器人、不连 QQ）。

用法（用 AstrBot 自带 venv 的 python）：
    D:\\DSH\\让大肥鱼活起来\\astrbot-tools\\astrbot\\Scripts\\python.exe tests\\import_check.py

检查：
1. 插件模块能在真实 astrbot 环境里导入（依赖、语法、相对导入）。
2. `inspect.signature(..., eval_str=True)` 能把 GreedyStr 注解解析成真实类，
   AstrBot 的指令解析才会把剩余文本当一整个参数。
3. on_llm_request 处理器的形参数量符合 (self, event, req)。
"""

from __future__ import annotations

import importlib.util
import inspect
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN_DIR = ROOT / "plugin" / "astrbot_plugin_feiyu_memory"

CANDIDATES = []
if os.environ.get("ASTRBOT_SITE_PACKAGES"):
    CANDIDATES.append(Path(os.environ["ASTRBOT_SITE_PACKAGES"]))
CANDIDATES += [
    ROOT / "plugins" / "astrbot" / "Lib" / "site-packages",
    ROOT / "astrbot-tools" / "astrbot" / "Lib" / "site-packages",
]
for candidate in CANDIDATES:
    if candidate.exists():
        sys.path.insert(0, str(candidate))
        break

try:
    import astrbot  # noqa: E402
except ModuleNotFoundError:
    print("找不到 AstrBot：把它的 site-packages 目录设进 ASTRBOT_SITE_PACKAGES 再跑。")
    raise SystemExit(2)

print("astrbot:", astrbot.__file__)

pkg_name = "astrbot_plugin_feiyu_memory"
spec = importlib.util.spec_from_file_location(
    f"{pkg_name}.main", PLUGIN_DIR / "main.py",
    submodule_search_locations=[str(PLUGIN_DIR)],
)
module = importlib.util.module_from_spec(spec)
sys.modules[f"{pkg_name}.main"] = module
spec.loader.exec_module(module)

plugin_cls = module.FeiyuMemoryPlugin
print("插件类:", plugin_cls.__name__)

sig = inspect.signature(plugin_cls.feiyu_command, eval_str=True)
params = list(sig.parameters)
assert params[:2] == ["self", "event"], params
assert len(params) == 3, params
annotation = sig.parameters["sub"].annotation
from astrbot.core.star.filter.command import GreedyStr  # noqa: E402

assert annotation is GreedyStr, annotation
print("GreedyStr 注解解析 ok：", annotation)

inject = inspect.signature(plugin_cls.inject_memory, eval_str=True)
assert list(inject.parameters) == ["self", "event", "req"], list(inject.parameters)
print("on_llm_request 形参 ok：", list(inject.parameters))

assert hasattr(plugin_cls, "initialize") and hasattr(plugin_cls, "terminate")

queue = inspect.signature(plugin_cls.queue_turn, eval_str=True)
assert list(queue.parameters) == ["self", "event", "response"], list(queue.parameters)
print("on_llm_response 形参 ok：", list(queue.parameters))

stage3 = inspect.signature(plugin_cls.maybe_write_diary, eval_str=True) \
    if hasattr(plugin_cls, "maybe_write_diary") else None
print("阶段三方法：", "有" if stage3 else "未暴露（用 _stage3_instance）")
print("导入检查通过。")

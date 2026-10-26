"""P1：本轮快照与「同一轮只注入一次」的小账本。

为什么单独一个文件：它不该依赖 astrbot、网络或磁盘，这样离线就能把
「一条消息触发多轮工具调用时会发生什么」测干净，而不是靠读代码推断。

两条不变量：
1. 同一条消息的后续轮次（工具轮）复用第一次算出来的动态块，不再检索第二次；
2. 同一个 req 对象只拼一次系统提示——否则多轮会把记忆块叠成两遍、三遍。
"""

from __future__ import annotations

from typing import Any


class TurnSnapshots:
    """按对象身份记账：不哈希内容，就不会把重复的一句话误当成同一轮。"""

    def __init__(self, max_entries: int = 16, enabled: bool = False) -> None:
        self.max_entries = max(1, int(max_entries))
        self.enabled = bool(enabled)
        self._snapshots: dict[int, tuple[Any, str]] = {}
        self._applied: dict[int, Any] = {}
        self.reused = 0
        self.applied = 0

    # -- 快照 ---------------------------------------------------------- #
    def remember(self, event: Any, blocks: str) -> None:
        """记下本轮块，供同一事件的后续轮次复用（关着就什么都不做）。"""
        if not self.enabled or event is None or not blocks:
            return
        self._snapshots[id(event)] = (event, blocks)
        while len(self._snapshots) > self.max_entries:
            self._snapshots.pop(next(iter(self._snapshots)))

    def reuse(self, event: Any) -> str | None:
        """同一事件的既有快照；没有就是 None（不跨事件复用）。"""
        if not self.enabled or event is None:
            return None
        entry = self._snapshots.get(id(event))
        if entry and entry[0] is event:
            self.reused += 1
            return entry[1]
        return None

    # -- 注入 ---------------------------------------------------------- #
    def already_applied(self, req: Any) -> bool:
        return self._applied.get(id(req)) is req

    def mark_applied(self, req: Any) -> None:
        self._applied[id(req)] = req
        while len(self._applied) > self.max_entries:
            self._applied.pop(next(iter(self._applied)))

    def apply(self, req: Any, blocks: str, persona: str = "") -> bool:
        """把块拼进 req.system_prompt；同一个 req 第二次调用直接返回 False。"""
        if req is None or not blocks:
            return False
        if self.already_applied(req):
            return False
        parts = []
        if persona:
            parts.append(persona)
        current = getattr(req, "system_prompt", "") or ""
        if current:
            parts.append(current)
        parts.append(blocks)
        req.system_prompt = "\n\n".join(parts)
        self.mark_applied(req)
        self.applied += 1
        return True

    def clear(self) -> None:
        self._snapshots.clear()
        self._applied.clear()

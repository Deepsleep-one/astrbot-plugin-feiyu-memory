"""阶段四：记忆整理（反思）。

提取是「只写不回看」，整理相反：它回看已有的记忆，做三件事——

1. 删重复：同一件事被记了两三遍，只留最新一条，其余归档（证据并入）；
2. 归纳：同一主题（同 fact_key）的零散观察，合并成一句更概括的话；
3. 不动声色：没有值得整理的就什么都不做，这是正常结果。

四条硬约束都在代码侧，不靠提示词自觉：
- 整理不产生新事实：输出必须与输入素材有明显词面重合，否则整条丢弃；
- 主人亲口说的话（owner_asserted）只删重复、绝不改写；
- 每条输出都必须带上来源记录 id，源记录被忘记时连带失效；
- 每次运行都写审计（``reflect_pass``），没把握就不动。

这个模块和 memory_stage3 一样只依赖标准库，模型通过 ``call_llm`` 传进来。
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from typing import Awaitable, Callable

from .memory_core import (
    MemoryStore,
    compact,
    iso_days_ago,
    now_dt,
    now_iso,
)

CallLLM = Callable[[str, str, str], Awaitable[str]]

# 只有这些类别会被整理：state 是易变的当前状态，diary 是叙述，都不该被归纳。
REFLECT_KINDS = ("preference", "profile", "fact", "relationship", "event")

# 允许被改写的来源类别：主人亲口说的话不在其中。
REWRITABLE_EPISTEMIC = ("derived", "observed_dialogue")

# 命中即整条丢弃（与提取器同一个口径，整理也不该把凭据搬来搬去）。
SENSITIVE_PATTERNS = [
    re.compile(r"密码|口令|密钥|私钥|验证码|短信码|银行卡|信用卡|身份证"),
    re.compile(r"(?i)\b(api[_-]?key|access[_-]?token|secret|passwd|password)\b"),
    re.compile(r"\bsk-[A-Za-z0-9]{8,}\b"),
    re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"),
]

REFLECT_SYSTEM = """你是一个记忆整理员。任务不是记录新事，而是把「已经记下的、同一个主题的零散条目」合并成一句更概括、更准确的话。

输出必须是且只能是一个 JSON 对象，不要解释、不要 Markdown 代码块：
{"merged":[{"fact_key":"...","content":"...","use":["id1","id2"]}]}

规则：
- 只允许使用给出的条目里已经存在的信息。不能补充、不能推测、不能把两件事脑补成因果。
- content 用中文陈述，2–60 字，语气与条目一致；宁可写得朴素，也不要写得更丰富。
- use 必须列出这条归纳实际用到的条目 id（至少两个），不够两个就别写这条输出。
- 只有当这几条确实在说同一件事、且合并后更清楚时才输出；否则输出 {"merged":[]}。
- 相互矛盾的说法不要合并：拿不准就输出 {"merged":[]}。

保持不变是合格结果，不是失败。"""


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #

def _norm(text: str) -> str:
    return re.sub(r"\s+", "", (text or "").lower())


def _sensitive(text: str) -> bool:
    return any(p.search(text or "") for p in SENSITIVE_PATTERNS)


def _strip_fences(text: str) -> str:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _bigrams(text: str) -> set[str]:
    """中文没有空格，分词容易把整句当一个词；用字二元组判断「说的是不是一回事」。"""
    s = re.sub(r"[\s\W_]+", "", (text or "").lower())
    if not s:
        return set()
    if len(s) == 1:
        return {s}
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _overlap(a: str, b: str) -> float:
    """重叠系数：∪/∩ 太严，短句会互相错过；按较短那句归一。"""
    sa, sb = _bigrams(a), _bigrams(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / min(len(sa), len(sb))


def parse_reflection(raw: str) -> list[dict]:
    """解析模型输出；任何异常都返回空列表（宁可不整理）。"""
    text = _strip_fences(raw)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return []
    try:
        data = json.loads(text[start:end + 1])
    except (json.JSONDecodeError, TypeError):
        return []
    items = data.get("merged") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        content = str(item.get("content") or "").strip()
        use = item.get("use")
        if not content or not isinstance(use, list):
            continue
        use = [str(u) for u in use if isinstance(u, str)]
        if not use:
            continue
        out.append({
            "content": content,
            "fact_key": str(item.get("fact_key") or "").strip(),
            "use": use,
        })
    return out


# --------------------------------------------------------------------------- #
# 整理器
# --------------------------------------------------------------------------- #

class MemoryReflector:
    """回看已有记忆并整理；所有写入都走 MemoryStore 的既有路径。"""

    def __init__(
        self,
        store: MemoryStore,
        call_llm: CallLLM,
        cfg: dict | None = None,
        logger=None,
        after_write: Callable[[str, str], None] | None = None,
    ) -> None:
        self.store = store
        self.call_llm = call_llm
        self.cfg = cfg or {}
        self.logger = logger
        self.after_write = after_write

    # -- 配置 ---------------------------------------------------------- #

    def _bool(self, key: str, default: bool) -> bool:
        value = self.cfg.get(key, default)
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)

    def _int(self, key: str, default: int) -> int:
        try:
            return int(self.cfg.get(key, default))
        except (TypeError, ValueError):
            return default

    def _float(self, key: str, default: float) -> float:
        try:
            return float(self.cfg.get(key, default))
        except (TypeError, ValueError):
            return default

    def _str(self, key: str, default: str = "") -> str:
        value = self.cfg.get(key, default)
        return str(value) if value is not None else default

    def _log(self, level: str, msg: str, *args) -> None:
        if self.logger is not None:
            try:
                getattr(self.logger, level, self.logger.info)(msg, *args)
            except Exception:
                pass

    def _owner_id(self) -> str:
        owners = self.store.list_owners()
        return owners[0]["owner_id"] if owners else "primary"

    # -- 主流程 -------------------------------------------------------- #

    async def run(self, *, force: bool = False) -> dict:
        """整理一轮；``force=True`` 时忽略时间窗与「今天已整理过」。"""
        result = {
            "ok": False, "reason": "", "deduped": 0, "merged": 0, "dropped": 0,
            "candidates": 0,
        }
        if not self._bool("reflect_enable", True):
            result["reason"] = "未开启"
            return result
        if not self.store.capture_enabled():
            result["reason"] = "记录已暂停"
            return result

        now = now_dt()
        today = now.strftime("%Y-%m-%d")
        hour = min(23, max(0, self._int("reflect_hour", 4)))
        if not force:
            if now.hour < hour:
                result["reason"] = "还没到点"
                return result
            if (self.store.get_control("last_reflect_date", "") or "") == today:
                result["reason"] = "今天已整理"
                return result

        owner_id = self._owner_id()
        days = max(1, self._int("reflect_days", 30))
        limit = max(4, self._int("reflect_batch", 120))
        recs = self.store.list_records(
            owner_id=owner_id,
            kinds=list(REFLECT_KINDS),
            since=iso_days_ago(days),
            limit=limit,
        )
        recs = [
            r for r in recs
            if r.get("scope_type") == "owner_private" and not _sensitive(r.get("content", ""))
        ]
        result["candidates"] = len(recs)
        if len(recs) < max(2, self._int("reflect_min_records", 4)):
            result["reason"] = f"素材不足（{len(recs)} 条）"
            result["ok"] = True
            self._finish(today, result)
            return result

        # 一、删重复：不做任何改写，只归档同义重复。
        try:
            deduped, dropped = self._dedupe(recs)
            result["deduped"] = deduped
            result["dropped"] += dropped
        except Exception as exc:
            result["reason"] = f"删重复失败：{type(exc).__name__}: {exc}"
            self._log("warning", "[feiyu-memory] 整理去重失败：%s", exc)

        # 二、归纳：说法相近的零散观察合并成一句（去重后重新取一遍，别拿旧的列表）。
        try:
            fresh = self.store.list_records(
                owner_id=owner_id,
                kinds=list(REFLECT_KINDS),
                since=iso_days_ago(days),
                limit=limit,
            )
            fresh = [
                r for r in fresh
                if r.get("scope_type") == "owner_private"
                and not _sensitive(r.get("content", ""))
            ]
            merged, dropped = await self._fold(owner_id, fresh, today)
            result["merged"] = merged
            result["dropped"] += dropped
        except Exception as exc:
            result["reason"] = f"归纳失败：{type(exc).__name__}: {exc}"
            self._log("warning", "[feiyu-memory] 整理归纳失败：%s", exc)

        result["ok"] = True
        self._finish(today, result)
        return result

    def _finish(self, today: str, result: dict) -> None:
        self.store.set_control("last_reflect_date", today, actor="system")
        self.store.set_control("last_reflect_at", now_iso(), actor="system")
        try:
            self.store.audit(
                "reflect_pass", actor="system",
                detail=(
                    f"候选 {result.get('candidates', 0)} 条，"
                    f"归档重复 {result.get('deduped', 0)} 条，"
                    f"归纳 {result.get('merged', 0)} 条，"
                    f"丢弃 {result.get('dropped', 0)} 条"
                ),
            )
        except Exception:
            pass

    # -- 一、去重 ------------------------------------------------------ #

    def _dedupe(self, recs: list[dict]) -> tuple[int, int]:
        """同一 (类别, 说法来源) 下高度相似的记录只留最新一条。"""
        threshold = max(0.5, min(0.99, self._float("reflect_dedupe", 0.88)))
        groups: dict[tuple, list[dict]] = {}
        for rec in recs:
            key = (
                rec.get("kind", ""),
                rec.get("epistemic_type", ""),
                rec.get("scope_type", ""),
                rec.get("scope_id", ""),
            )
            groups.setdefault(key, []).append(rec)

        archives = 0
        dropped = 0
        for group in groups.values():
            if len(group) < 2:
                continue
            # 新的在前：保留最新的说法，旧说法归档。
            group = sorted(
                group, key=lambda r: (r.get("asserted_at") or "", r.get("revision") or 0),
                reverse=True,
            )
            kept: list[dict] = []
            for rec in group:
                same = None
                for cand in kept:
                    if _norm(cand["content"]) == _norm(rec["content"]) \
                            or _overlap(cand["content"], rec["content"]) >= threshold:
                        same = cand
                        break
                if same is None:
                    kept.append(rec)
                    continue
                merged = self.store.merge_records(
                    same["id"], [rec["id"]], reason="reflect_dedupe"
                )
                archives += merged
                if not merged:
                    dropped += 1
        return archives, dropped

    # -- 二、归纳 ------------------------------------------------------ #

    def _cluster(self, recs: list[dict]) -> list[list[dict]]:
        """按说法相近聚类：同类别、内容有交集但不是重复的那几条归一组。

        不能按 fact_key 分组——单值唯一索引保证同一 fact_key 只会有一条 active，
        所以「同主题的零散观察」在库里本来就散落在不同 fact_key（甚至没有）上。
        """
        threshold = max(0.2, min(0.8, self._float("reflect_fold", 0.45)))
        by_kind: dict[str, list[dict]] = {}
        for rec in recs:
            if rec.get("epistemic_type") not in REWRITABLE_EPISTEMIC:
                continue
            if (rec.get("status") or "active") != "active":
                continue
            by_kind.setdefault(rec.get("kind", ""), []).append(rec)

        clusters: list[list[dict]] = []
        for group in by_kind.values():
            group = sorted(
                group, key=lambda r: (r.get("asserted_at") or "", r.get("id") or ""),
                reverse=True,
            )
            used: set[str] = set()
            for rec in group:
                if rec["id"] in used:
                    continue
                cluster = [rec]
                used.add(rec["id"])
                for other in group:
                    if other["id"] in used:
                        continue
                    if max(
                        _overlap(m["content"], other["content"]) for m in cluster
                    ) >= threshold:
                        cluster.append(other)
                        used.add(other["id"])
                if len(cluster) >= 2:
                    clusters.append(cluster)
        return clusters

    async def _fold(self, owner_id: str, recs: list[dict], today: str) -> tuple[int, int]:
        clusters = self._cluster(recs)
        min_fold = max(2, self._int("reflect_min_fold", 2))
        total_merged = 0
        dropped = 0
        for cluster in clusters:
            if len(cluster) < min_fold:
                continue
            cluster = cluster[: max(2, self._int("reflect_group_limit", 8))]
            kind = cluster[0].get("kind", "")
            by_id = {r["id"]: r for r in cluster}
            # 组里出现过的 fact_key 里挑最常见的那个；都没有就留空。
            keys = [str(r.get("fact_key") or "").strip() for r in cluster]
            keys = [k for k in keys if k]
            fact_key = max(set(keys), key=keys.count) if keys else ""
            materials = "\n".join(
                f"- id={r['id']}｜{compact(r['content'], 120)}" for r in cluster
            )
            prompt = (
                f"类别：{kind}\n\n已记录的条目：\n{materials}"
            )
            try:
                raw = await self.call_llm(
                    REFLECT_SYSTEM, prompt, self._str("reflect_model", "")
                )
            except Exception as exc:
                self._log("warning", "[feiyu-memory] 整理调用模型失败：%s", exc)
                continue

            for item in parse_reflection(raw):
                content = item["content"]
                use = [u for u in item["use"] if u in by_id]
                if not use or len(use) < 2:
                    dropped += 1
                    continue
                if not 2 <= len(content) <= 80:
                    dropped += 1
                    continue
                if _sensitive(content):
                    dropped += 1
                    continue
                sources = " ".join(by_id[u]["content"] for u in use)
                if _overlap(content, sources) < 0.3:
                    # 词面几乎没有重合：多半是模型自己编的，不要写进库。
                    dropped += 1
                    continue
                importance = max(int(by_id[u].get("importance") or 1) for u in use)
                digest = hashlib.sha1(
                    f"{today}|{fact_key}|{content}".encode("utf-8")
                ).hexdigest()[:16]
                try:
                    rec = self.store.add_record(
                        owner_id=owner_id,
                        kind=kind,
                        content=content,
                        fact_key=item["fact_key"] or fact_key,
                        epistemic_type="derived",
                        scope_type="owner_private",
                        source_ids=list(use),
                        derived_from_ids=list(use),
                        importance=min(5, importance),
                        confidence=0.6,
                        created_by="reflect",
                        auto_supersede=True,
                        supersede_epistemic=REWRITABLE_EPISTEMIC,
                        op_key=f"reflect:{digest}",
                    )
                except sqlite3.IntegrityError:
                    # 同一主题已经有主人本人确认的记录：归纳让位，不覆盖原话。
                    dropped += 1
                    continue
                except Exception as exc:
                    self._log("warning", "[feiyu-memory] 整理写入失败：%s", exc)
                    dropped += 1
                    continue
                if rec.get("_idempotent"):
                    continue
                # 归纳出来的这条取代被它归纳的那几条（不动主人原话）。
                try:
                    self.store.merge_records(rec["id"], list(use),
                                             reason="reflect_fold")
                except Exception as exc:
                    self._log("debug", "[feiyu-memory] 归纳归档失败：%s", exc)
                total_merged += 1
                if self.after_write is not None:
                    try:
                        self.after_write(rec["id"], content)
                    except Exception:
                        pass
        return total_merged, dropped

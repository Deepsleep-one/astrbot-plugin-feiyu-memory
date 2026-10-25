"""阶段三：后台自动提取、关系日记、工作状态。

这个模块只依赖标准库，也不直接碰 AstrBot：调用方传入一个 ``call_llm``
协程（system_prompt, user_prompt, model -> str），所以可以在自测里用假模型
把整条链路跑通。

三道闸门都在代码侧，而不是靠提示词自觉：
1. 自动提取只能写 ``observed_dialogue``，且证据必须能在原对话里找到；
2. 置信度低于阈值、命中敏感信息、或与已有记录重复的候选一律丢弃；
3. 关系日记是 ``derived``，只在私聊注入；源记录被忘记/纠正后连带失效。
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import timedelta
from typing import Awaitable, Callable

from .memory_core import (
    MemoryError,
    MemoryStore,
    compact,
    now_dt,
    now_iso,
    text_similarity,
)

CallLLM = Callable[[str, str, str], Awaitable[str]]

# 提取器允许写出的类别。lore 只能来自预设，diary 只能由日记任务写。
EXTRACT_KINDS = (
    "preference",
    "profile",
    "fact",
    "event",
    "commitment",
    "relationship",
    "state",
)

DIARY_SOURCE_KINDS = (
    "preference",
    "profile",
    "fact",
    "event",
    "commitment",
    "relationship",
)

# 命中即整条丢弃：这些内容不进入记忆库，避免把凭据写进备份。
SENSITIVE_PATTERNS = [
    re.compile(r"密码|口令|密钥|私钥|验证码|短信码|银行卡|信用卡|身份证"),
    re.compile(r"(?i)\b(api[_-]?key|access[_-]?token|secret|passwd|password)\b"),
    re.compile(r"\bsk-[A-Za-z0-9]{8,}\b"),
    re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"),
]

EXTRACT_SYSTEM = """你是一个记忆提取器，只做一件事：从一段主人与助手的真实对话里，找出「值得长期记住的、关于主人的稳定信息」。

输出必须是且只能是一个 JSON 对象，不要解释、不要 Markdown 代码块：
{"memories":[{"kind":"...","content":"...","fact_key":"...","confidence":0.0,"evidence":"...","scope":"private"}]}

字段规则：
- kind 只能是：preference（长期偏好/口味/习惯）、profile（现实资料：职业、作息、身体、家庭）、fact（其他稳定事实）、event（真实发生过的共同经历）、commitment（主人明确答应过或要求提醒的事）、relationship（对关系的明确表态）、state（当前正在做的事，几天内有效）。
- content：用中文陈述，2–60 字，写主人本人或真实发生的事；不要写助手的性格、不要写寒暄、不要写命令式请求。
- fact_key：可选，同类主题用同一个短英文键（如 spice、sleep、job），用于新旧信息取代；不确定就留空。
- confidence：0–1，只有对话明确支持时才 ≥0.7。
- evidence：必须是从对话里原样摘出的一小段（10–60 字），能被逐字找到；找不到就整条不要输出。
- scope：默认 private；只有主人明确说可以公开时才写 public。

不要提取：密码/密钥/验证码/联系方式等敏感信息、玩笑和反串、假设与如果、助手说过的话、对未来的空泛承诺、一次性的情绪。
没有值得记的就输出 {"memories":[]}。"""

DIARY_SYSTEM = """你是大肥鱼的关系日记作者。根据给出的「今天真实发生的记忆条目」，写 1–3 句中文日记。

要求：
- 第一人称，像自己在回想今天，可以提具体的事，但不要罗列条目。
- 只写实际发生的互动和主人已经表达过的反应；不推断没说出口的想法，不编造细节，不夸张。
- 不写系统、编号、数据库、提示词这类词。
- 素材很少时，就写一句朴素的，比如「今天没聊太多，不过他说不吃辣这事我记住了。」
- 只输出日记正文，不要标题、不要引号、不要解释。"""


# --------------------------------------------------------------------------- #
# 文本工具
# --------------------------------------------------------------------------- #

def _norm(text: str) -> str:
    return re.sub(r"\s+", "", (text or "").lower())


def _similar(a: str, b: str) -> float:
    """两句话是不是同一件事：与库里检索/去重共用同一个口径。"""
    return text_similarity(a, b)


def _strip_fences(text: str) -> str:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def parse_extraction(raw: str) -> list:
    """把模型输出解析成候选列表；任何解析失败都返回空列表（宁可不记）。"""
    text = _strip_fences(raw)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return []
    try:
        data = json.loads(text[start:end + 1])
    except (json.JSONDecodeError, TypeError):
        return []
    items = data.get("memories") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def _sensitive(text: str) -> bool:
    return any(p.search(text or "") for p in SENSITIVE_PATTERNS)


_NEGATIONS = ("不", "没", "讨厌", "拒绝", "从不", "不再")

# 证据必须落在主人的话里；助手自述再像也不是主人的事实。
_SPEAKER_PREFIXES = (
    ("主人说：", "owner"),
    ("主人说:", "owner"),
    ("主人：", "owner"),
    ("主人:", "owner"),
    ("助手说：", "assistant"),
    ("助手说:", "assistant"),
    ("助手：", "assistant"),
    ("助手:", "assistant"),
    ("大肥鱼说：", "assistant"),
    ("大肥鱼说:", "assistant"),
    ("大肥鱼：", "assistant"),
    ("大肥鱼:", "assistant"),
)


def _polarity(text: str) -> int:
    """粗判肯/否极性，只用来拦住「喜欢 / 不喜欢」这类整句翻转。"""
    low = (text or "").lower()
    return -1 if any(tok in low for tok in _NEGATIONS) else 1


def _norm_map(text: str) -> tuple:
    """归一化（去空白 + 小写），同时记下每个归一字回到原文的下标。"""
    chars: list = []
    index: list = []
    for i, ch in enumerate(text or ""):
        if ch.isspace():
            continue
        chars.append(ch.lower())
        index.append(i)
    return "".join(chars), index


def _locate_span(evidence: str, text: str) -> tuple | None:
    """在原文里逐字定位证据，返回原文坐标 (start, end)；定位不到返回 None。

    只容忍空白差异：近义改写、否定改写一律不算「原文里说过」。
    """
    ev, _ = _norm_map(evidence)
    if len(ev) < 4 or not (text or "").strip():
        return None
    body, index = _norm_map(text)
    pos = body.find(ev)
    if pos < 0:
        return None
    return index[pos], index[pos + len(ev) - 1] + 1


def _evidence_supported(evidence: str, transcript: str) -> bool:
    """证据必须是对话原文的一部分，不许用相似度冒充。"""
    return _locate_span(evidence, transcript) is not None


def _parse_transcript_messages(transcript: str) -> list:
    """把「主人：…/助手：…」拆成带说话人的消息。

    一条前缀都认不出来时返回空列表，交给调用方按「单说话者」处理（兼容旧调用）。
    """
    messages: list = []
    unknown: list = []
    for line in (transcript or "").splitlines():
        body = line.strip()
        if not body:
            continue
        for prefix, role in _SPEAKER_PREFIXES:
            if body.startswith(prefix):
                messages.append({"role": role, "text": body[len(prefix):].strip()})
                break
        else:
            unknown.append(body)
    if not messages:
        return []
    messages.extend({"role": "unknown", "text": body} for body in unknown)
    return messages


def _evidence_message(messages: list, evidence: str) -> tuple:
    """找出证据落在哪条消息里；主人和助手都说过同一句时，优先算主人的。"""
    for only_owner in (True, False):
        for msg in messages:
            if only_owner and msg.get("role") != "owner":
                continue
            span = _locate_span(evidence, msg.get("text", ""))
            if span:
                return msg, span
    return None, None


def _content_borrowed(messages: list, content: str) -> bool:
    """候选内容只出现在助手嘴里而主人没说过 —— 那它就不是主人的事实。"""
    needle = _norm(content)
    if len(needle) < 2:
        return False
    roles = {
        msg.get("role") for msg in messages if needle in _norm(msg.get("text", ""))
    }
    return roles == {"assistant"}


def _sanitize_fact_key(key: str) -> str:
    key = (key or "").strip().lower()
    key = re.sub(r"[^a-z0-9_.\-]", "", key)
    return key[:40]


def _age_minutes(iso: str | None) -> float:
    if not iso:
        return 0.0
    try:
        from datetime import datetime

        started = datetime.fromisoformat(iso)
        return max(0.0, (now_dt() - started).total_seconds() / 60.0)
    except Exception:
        return 0.0


# --------------------------------------------------------------------------- #
# 阶段三编排
# --------------------------------------------------------------------------- #

class Stage3:
    def __init__(
        self,
        store: MemoryStore,
        cfg: dict,
        call_llm: CallLLM,
        *,
        logger=None,
        after_write: Callable[[str, str], None] | None = None,
    ):
        self.store = store
        self.cfg = cfg if isinstance(cfg, dict) else {}
        self.call_llm = call_llm
        self.logger = logger
        self.after_write = after_write

    # -- 配置 ---------------------------------------------------------- #

    def _bool(self, key: str, default: bool) -> bool:
        value = self.cfg.get(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on", "启用", "开启"}
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

    # -- 入队 ---------------------------------------------------------- #

    def capture_turn(
        self,
        *,
        session_id: str,
        user_text: str,
        assistant_text: str,
        ident: dict,
    ) -> str | None:
        """把一轮成功对话放进提取队列；群聊与非主人一律跳过。"""
        if not self._bool("auto_extract", False):
            return None
        if not ident.get("is_owner") or ident.get("scene") != "private":
            return None
        if not self.store.capture_enabled():
            return None
        user_text = (user_text or "").strip()
        assistant_text = (assistant_text or "").strip()
        if len(user_text) < 2 or not assistant_text:
            return None
        owner = ident.get("owner") or {}
        payload = {
            "owner_id": owner.get("owner_id", "primary"),
            "platform": ident.get("platform", ""),
            "bot_instance": ident.get("bot_instance", ""),
            "user_id": ident.get("user_id", ""),
            "session_id": session_id or "",
            "user": compact(user_text, 1200),
            "assistant": compact(assistant_text, 1200),
            # 结构化的一轮对话：提取时据此判定证据是谁说的，而不是靠字符串前缀。
            "turns": [
                {"role": "owner", "text": compact(user_text, 1200)},
                {"role": "assistant", "text": compact(assistant_text, 1200)},
            ],
            "ts": now_iso(),
            "epoch": self.store.deletion_epoch(),
        }
        digest = hashlib.sha1(
            f"{session_id}|{payload['user']}|{payload['assistant']}".encode("utf-8")
        ).hexdigest()[:24]
        return self.store.enqueue_job(
            "extract", payload, dedupe_key=f"turn:{digest}"
        )

    # -- 提取 ---------------------------------------------------------- #

    async def process_pending(self, *, force: bool = False) -> dict:
        """按批处理待提取对话；未到批量/空闲阈值且非 force 时直接返回。"""
        result = {"processed": 0, "written": 0, "skipped": 0, "jobs": 0}
        if not self._bool("auto_extract", False):
            return result
        if not self.store.capture_enabled():
            return result
        count, oldest = self.store.pending_job_span()
        if count <= 0:
            return result
        batch = max(1, self._int("extract_batch_turns", 6))
        idle = max(1, self._int("extract_idle_minutes", 5))
        if not force and count < batch and _age_minutes(oldest) < idle:
            return result
        if self._daily_quota_left() <= 0:
            self._log("info", "[feiyu-memory] 今日提取配额已用完，等待明天")
            return result

        claim = min(max(count, batch), batch * 2, 12)
        jobs = self.store.claim_jobs(limit=claim)
        result["jobs"] = len(jobs)
        if not jobs:
            return result

        groups: dict[tuple[str, str, str], list[dict]] = {}
        for job in jobs:
            p = job.get("payload") or {}
            key = (
                p.get("owner_id", "primary"),
                p.get("platform", ""),
                p.get("bot_instance", ""),
            )
            groups.setdefault(key, []).append(job)

        for (owner_id, platform, bot_instance), group in groups.items():
            try:
                written, skipped = await self._extract_group(
                    owner_id, platform, bot_instance, group
                )
            except Exception as exc:  # 整批失败：退回队列，最多重试三次
                for job in group:
                    self.store.fail_job(job["id"], f"{type(exc).__name__}: {exc}")
                self._log("warning", "[feiyu-memory] 提取批次失败：%s", exc)
                continue
            result["written"] += written
            result["skipped"] += skipped
            result["processed"] += len(group)

        return result

    async def _extract_group(
        self, owner_id: str, platform: str, bot_instance: str, jobs: list[dict]
    ) -> tuple[int, int]:
        transcript = "\n".join(
            f"主人：{job['payload'].get('user', '')}\n"
            f"助手：{job['payload'].get('assistant', '')}"
            for job in jobs
        )
        turns: list = []
        for job in jobs:
            payload = job.get("payload") or {}
            stored = payload.get("turns")
            if isinstance(stored, list) and stored:
                for item in stored:
                    if isinstance(item, dict) and str(item.get("text") or "").strip():
                        turns.append({
                            "role": item.get("role") or "unknown",
                            "text": str(item.get("text")),
                            "job": job["id"],
                        })
            else:
                # 老队列里的 payload 没有 turns，按「主人 + 助手」两条补回来。
                if str(payload.get("user") or "").strip():
                    turns.append({
                        "role": "owner", "text": str(payload.get("user")), "job": job["id"],
                    })
                if str(payload.get("assistant") or "").strip():
                    turns.append({
                        "role": "assistant",
                        "text": str(payload.get("assistant")),
                        "job": job["id"],
                    })
        raw = await self.call_llm(
            EXTRACT_SYSTEM, transcript, self._str("extract_model", "")
        )
        candidates = parse_extraction(raw)
        written = 0
        skipped = 0
        for cand in candidates:
            if written >= self._int("extract_batch_turns", 6):
                break
            try:
                rec = self._write_candidate(
                    cand, owner_id, platform, bot_instance, transcript, jobs[0],
                    messages=turns,
                )
            except MemoryError as exc:
                skipped += 1
                self._log("debug", "[feiyu-memory] 候选被拒：%s", exc)
                continue
            if rec is None:
                skipped += 1
                continue
            if rec.get("_idempotent"):
                # 这一条之前就写过了（重跑/重复投递），不算新写。
                skipped += 1
                continue
            written += 1
        if written:
            self._bump_daily(written)
        for job in jobs:
            self.store.finish_job(job["id"], f"写 {written} 条，跳过 {skipped} 条")
        return written, skipped

    def _write_candidate(
        self,
        cand: dict,
        owner_id: str,
        platform: str,
        bot_instance: str,
        transcript: str,
        job: dict,
        messages: list | None = None,
    ) -> dict | None:
        kind = str(cand.get("kind", "")).strip().lower()
        if kind not in EXTRACT_KINDS:
            raise MemoryError(f"未知类别 {kind}")
        content = str(cand.get("content", "")).strip()
        if not 2 <= len(content) <= 120:
            raise MemoryError("内容长度不合规")
        if _sensitive(content):
            raise MemoryError("命中敏感信息")
        try:
            confidence = float(cand.get("confidence", 0))
        except (TypeError, ValueError):
            confidence = 0.0
        min_conf = self._float("extract_min_confidence", 0.7)
        if confidence < min_conf:
            raise MemoryError(f"置信度 {confidence:.2f} 低于 {min_conf:.2f}")
        evidence = str(cand.get("evidence", "")).strip()
        if not evidence:
            raise MemoryError("证据无法在原对话中定位")

        # 证据归属：必须落在主人自己说的话里。旧调用没有结构化 messages 时，
        # 从带前缀的 transcript 还原；连前缀都认不出才按单说话者兼容。
        turns = messages if messages is not None else _parse_transcript_messages(transcript)
        single = not turns
        if single:
            turns = [{"role": "owner", "text": transcript or ""}]
        msg, span = _evidence_message(turns, evidence)
        if msg is None:
            raise MemoryError("证据无法在原对话中定位")
        if msg.get("role") != "owner":
            raise MemoryError("证据属于助手自述，不能当作主人的事实")
        if not single and _content_borrowed(turns, content):
            raise MemoryError("证据属于助手自述，不能当作主人的事实")
        if _sensitive(evidence):
            raise MemoryError("证据命中敏感信息")

        # 旧材料抑制：主人要求忘掉过的内容，不许被同一段旧对话重新提取回来。
        for text in (content, evidence):
            if self.store.tombstoned(text):
                self.store.audit(
                    "tombstone_block",
                    actor="extractor",
                    detail=compact(content, 60),
                )
                raise MemoryError("这条内容已被主人要求遗忘，不再重新记入")

        scope_hint = str(cand.get("scope", "private")).strip().lower()
        scope_type = "owner_public" if (
            scope_hint == "public" and self._bool("allow_auto_public", False)
        ) else "owner_private"

        # 重复检测：完全一致才算重复；高度相似时先看极性，反话不能被当成重复吞掉。
        for rec in self.store.find_similar(owner_id, content, limit=40):
            if _norm(rec["content"]) == _norm(content):
                raise MemoryError("与已有记录重复")
            similarity = float(rec.get("similarity") or 0)
            if similarity < 0.6:
                continue
            if _polarity(rec["content"]) != _polarity(content):
                self.store.audit(
                    "extract_conflict",
                    actor="extractor",
                    record_id=rec["id"],
                    detail=(
                        "极性相反的近似观察，待确认："
                        f"{compact(rec['content'], 60)} | {compact(content, 60)}"
                    ),
                )
                raise MemoryError("与已有记录极性相反，已记为待确认冲突")
            if similarity >= 0.85:
                raise MemoryError("与已有记录高度相似")

        fact_key = _sanitize_fact_key(str(cand.get("fact_key", "")))
        if kind == "state":
            if self.store.find_active(
                owner_id, kind="state", epistemic_type="owner_asserted", limit=1
            ):
                raise MemoryError("已有主人手动设定的当前状态，自动提取不覆盖")
        if fact_key:
            blockers = self.store.find_active(
                owner_id, fact_key=fact_key, epistemic_type="owner_asserted", limit=3
            )
            if blockers:
                # 主人明确说过的版本优先；自动提取不覆盖，但要留痕，不能静默丢。
                self.store.audit(
                    "extract_conflict",
                    actor="extractor",
                    record_id=blockers[0]["id"],
                    detail=(
                        f"fact_key={fact_key} 已有主人确认的说法："
                        f"{compact(blockers[0]['content'], 60)}；本次候选丢弃："
                        f"{compact(content, 60)}"
                    ),
                )
                raise MemoryError("已有主人本人确认的同主题记录（已记为待确认冲突）")

        expires_at = None
        if kind == "state":
            ttl = max(1, self._int("state_ttl_days", 7))
            expires_at = (now_dt() + timedelta(days=ttl)).isoformat()

        rec = self.store.add_record(
            owner_id=owner_id,
            kind=kind,
            content=content,
            epistemic_type="observed_dialogue",
            scope_type=scope_type,
            scope_id="",
            fact_key=fact_key or (f"auto.{kind}" if kind == "state" else ""),
            importance=4 if kind in ("state", "commitment") else 3,
            confidence=min(0.95, max(0.5, confidence)),
            expires_at=expires_at,
            platform=platform,
            bot_instance=bot_instance,
            created_by="extractor",
            source_ids=[str(job["id"])],
            evidence=[{
                "kind": "dialogue",
                "ref": f"job:{job['id']}#m{next((i for i, t in enumerate(turns) if t is msg), 0)}",
                "excerpt": evidence[:500],
                "speaker": "owner",
                "content_hash": hashlib.sha256(evidence.encode("utf-8")).hexdigest()[:32],
                "span_start": span[0],
                "span_end": span[1],
            }],
            # 幂等键：同一轮对话重跑（任务重试、批次重复投递）不会写第二条，
            # 也不用靠 0.85 相似度去猜——那会把「吃了麻辣香锅」和「吃了酸菜鱼」判成同一条。
            op_key="extract:" + hashlib.sha1(
                f"{job['id']}|{kind}|{content}".encode("utf-8")
            ).hexdigest()[:20],
        )
        if self.after_write is not None:
            try:
                self.after_write(rec["id"], content)
            except Exception:
                pass
        return rec

    # -- 每日配额 ------------------------------------------------------ #

    def _today(self) -> str:
        return now_dt().strftime("%Y-%m-%d")

    def _daily_quota_left(self) -> int:
        raw = self.store.get_control("extract_daily", "") or ""
        day, _, used = raw.partition(":")
        used_n = int(used) if used.isdigit() else 0
        if day != self._today():
            return max(0, self._int("extract_daily_limit", 40))
        return max(0, self._int("extract_daily_limit", 40) - used_n)

    def _bump_daily(self, n: int) -> None:
        if n <= 0:
            return
        raw = self.store.get_control("extract_daily", "") or ""
        day, _, used = raw.partition(":")
        used_n = int(used) if used.isdigit() else 0
        if day != self._today():
            used_n = 0
        self.store.set_control(
            "extract_daily", f"{self._today()}:{used_n + n}", actor="system"
        )

    # -- 关系日记 ------------------------------------------------------ #

    async def maybe_write_diary(self, *, force: bool = False) -> dict:
        result = {"written": False, "reason": ""}
        if not self._bool("diary_enable", True):
            result["reason"] = "未开启"
            return result
        if not self.store.capture_enabled():
            result["reason"] = "记录已暂停"
            return result

        now = now_dt()
        today = now.strftime("%Y-%m-%d")
        hour = min(23, max(0, self._int("diary_hour", 23)))
        minute = min(59, max(0, self._int("diary_minute", 30)))
        if not force:
            target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if now < target:
                result["reason"] = "还没到点"
                return result
            if (self.store.get_control("last_diary_date", "") or "") == today:
                result["reason"] = "今天已写"
                return result

        since = self.store.get_control("last_diary_at", "") or (
            now - timedelta(hours=24)
        ).isoformat()
        recs = self.store.list_records(
            owner_id=self._owner_id(),
            kinds=list(DIARY_SOURCE_KINDS),
            since=since,
            limit=80,
        )
        recs = [
            r for r in recs
            if r["scope_type"] == "owner_private"
            and r["epistemic_type"] in ("owner_asserted", "observed_dialogue")
        ]
        min_records = max(1, self._int("diary_min_records", 3))
        if len(recs) < min_records and not force:
            result["reason"] = f"素材不足（{len(recs)} 条）"
            return result
        if not recs:
            result["reason"] = "今天没有可用的素材"
            return result

        materials = "\n".join(
            f"- [{r['kind']}] {compact(r['content'], 120)}"
            for r in recs[:40]
        )
        text = await self.call_llm(
            DIARY_SYSTEM, materials, self._str("diary_model", "")
        )
        text = _strip_fences(text).strip()
        text = re.sub(r"\s+", " ", text)
        if not 4 <= len(text) <= 400:
            result["reason"] = "日记长度异常，丢弃"
            return result
        rec = self.store.add_record(
            owner_id=self._owner_id(),
            kind="diary",
            content=text,
            epistemic_type="derived",
            scope_type="owner_private",
            derived_from_ids=[r["id"] for r in recs],
            importance=2,
            confidence=0.5,
            created_by="diary",
        )
        if self.after_write is not None:
            try:
                self.after_write(rec["id"], text)
            except Exception:
                pass
        self.store.set_control("last_diary_at", now_iso(), actor="system")
        self.store.set_control("last_diary_date", today, actor="system")
        self.store.audit("diary_write", actor="system", record_id=rec["id"],
                         detail=compact(text, 80))
        result["written"] = True
        result["record_id"] = rec["id"]
        return result

    def _owner_id(self) -> str:
        owners = self.store.list_owners()
        if owners:
            return owners[0]["owner_id"]
        return "primary"

    # -- 周期任务 ------------------------------------------------------ #

    async def tick(self) -> dict:
        out: dict = {}
        try:
            out["expired"] = self.store.expire_states()
        except Exception as exc:
            self._log("warning", "[feiyu-memory] 过期状态清理失败：%s", exc)
        try:
            out["requeued"] = self.store.requeue_stale_jobs()
        except Exception as exc:
            self._log("warning", "[feiyu-memory] 悬挂任务回收失败：%s", exc)
        if self._bool("auto_extract", False):
            try:
                out["extract"] = await self.process_pending()
            except Exception as exc:
                self._log("warning", "[feiyu-memory] 自动提取失败：%s", exc)
        if self._bool("diary_enable", True):
            try:
                out["diary"] = await self.maybe_write_diary()
            except Exception as exc:
                self._log("warning", "[feiyu-memory] 日记生成失败：%s", exc)
        return out

    # -- 状态写入（供指令/CLI 复用） ----------------------------------- #

    def set_work_state(
        self,
        *,
        owner_id: str,
        content: str,
        ident: dict | None = None,
        ttl_days: int | None = None,
    ) -> dict:
        ident = ident or {}
        return self.store.set_state(
            owner_id=owner_id,
            content=content,
            ttl_days=ttl_days if ttl_days is not None else self._int("state_ttl_days", 7),
            platform=ident.get("platform", ""),
            bot_instance=ident.get("bot_instance", ""),
            created_by="owner",
            evidence=[{
                "kind": "owner_message",
                "ref": f"{ident.get('platform', '')}:{ident.get('user_id', '')}:command",
                "excerpt": compact(content, 200),
            }],
        )

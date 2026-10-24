#!/usr/bin/env python
"""大肥鱼长期记忆 · 本地管理 CLI（只用标准库）

身份绑定、核心设定、档案导入、查看、纠正、忘记、备份/恢复都在这里做。
聊天里的“我是主人”永远不会被当成绑定。

常用：
    python tools\\feiyu_memory_admin.py template                 # 生成待填档案
    python tools\\feiyu_memory_admin.py bind-owner --user-id 123456
    python tools\\feiyu_memory_admin.py import 主人档案填写.md    # 先预览
    python tools\\feiyu_memory_admin.py import 主人档案填写.md --apply
    python tools\\feiyu_memory_admin.py status
    python tools\\feiyu_memory_admin.py list
    python tools\\feiyu_memory_admin.py backup
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_DIR = PROJECT_ROOT / "plugin" / "astrbot_plugin_feiyu_memory"
sys.path.insert(0, str(PLUGIN_DIR))

from memory_core import (  # noqa: E402
    CORE_SECTIONS,
    EPISTEMIC_TAG,
    KINDS,
    MemoryError,
    MemoryStore,
    compact,
    default_db_path,
    now_iso,
)

DEFAULT_TEMPLATE = PROJECT_ROOT / "主人档案填写.md"

FACT_KIND_MAP = {
    "喜好": "preference",
    "偏好": "preference",
    "习惯": "preference",
    "口味": "preference",
    "背景": "profile",
    "工作": "profile",
    "工作学习背景": "profile",
    "资料": "profile",
    "其他": "profile",
}

RELATION_KEYS = {
    "关系背景": ("relation", "关系背景"),
    "关系": ("relation", "关系背景"),
    "亲近与撒娇程度": ("relation", "亲近程度"),
    "亲近程度": ("relation", "亲近程度"),
    "能接受的调侃": ("boundary", "能接受的调侃"),
    "不可接受的称呼或动作描写": ("boundary", "不可接受的称呼"),
    "不可接受的称呼": ("boundary", "不可接受的称呼"),
    "不接受的称呼": ("boundary", "不可接受的称呼"),
    "不可接受的": ("boundary", "不可接受的称呼"),
    "需要认真对待的边界": ("boundary", "边界"),
    "边界": ("boundary", "边界"),
    "希望她怎样安慰你": ("preference", "安慰方式"),
    "安慰方式": ("preference", "安慰方式"),
}


# --------------------------------------------------------------------------- #
# 输出小工具
# --------------------------------------------------------------------------- #

def out(text: str = "") -> None:
    print(text)


def mask_uid(uid: str) -> str:
    uid = str(uid)
    if len(uid) <= 4:
        return uid
    return uid[:2] + "*" * (len(uid) - 4) + uid[-2:]


# --------------------------------------------------------------------------- #
# 档案模板
# --------------------------------------------------------------------------- #

TEMPLATE_TEXT = """# 大肥鱼主人档案（填写后导入）

规则：
1. 只填等号（=）右边；空着的项会被忽略，不会写成记忆。
2. 不要填密码、API Key、证件号、住址。
3. 可见范围只有两种：私聊（群里绝对看不到） / 公开（主人进群时可用）。
4. `#` 开头的行是注释，不会被导入。

## 称呼

私聊称呼 = 
群内称呼 = 

## 关系

关系背景 = 
亲近程度 = 
能接受的调侃 = 
不可接受的称呼 = 
安慰方式 = 
边界 = 

## 核心事实

# 每行一条：类别 | 内容 | 范围
# 类别可写：喜好 / 习惯 / 背景 / 其他
# 下面两行是示例，以 # 开头不会导入；照着格式另起行即可：
# 喜好 | 不吃辣 | 私聊
# 背景 | 做后端开发 | 公开

## 约定

# 每行一条：内容 | 时间要求（时间可留空）
# 例：
# 周日前把记忆插件装好 | 本周日
"""


def cmd_template(args) -> int:
    target = Path(args.out) if args.out else DEFAULT_TEMPLATE
    if target.exists() and not args.force:
        out(f"已存在：{target}")
        out("要覆盖请加 --force。")
        return 0
    target.write_text(TEMPLATE_TEXT, encoding="utf-8")
    out(f"已生成：{target}")
    out("填完先跑 dry-run 预览：")
    out(f"  python tools\\feiyu_memory_admin.py import \"{target.name}\"")
    out("确认无误再加 --apply。")
    return 0


# --------------------------------------------------------------------------- #
# 解析填写文件
# --------------------------------------------------------------------------- #

def parse_profile(text: str) -> dict:
    result: dict = {
        "address": {},      # title -> content
        "relation": {},     # (section, title) -> content
        "facts": [],        # (kind, content, scope)
        "commitments": [],  # (content, time)
        "warnings": [],
    }
    section = ""
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("##"):
            section = line.lstrip("#").strip()
            continue
        if line.startswith("["):
            section = line.strip("[]").strip()
            continue
        if line.startswith("#"):
            continue
        if not section:
            # 第一个区块之前的说明文字（规则、标题）不参与解析
            continue

        if "|" in line and section in ("核心事实", "事实", "约定"):
            cells = [c.strip() for c in line.split("|")]
            if section in ("核心事实", "事实"):
                if len(cells) < 2 or not cells[1]:
                    continue
                kind_label, content = cells[0], cells[1]
                scope = cells[2] if len(cells) > 2 and cells[2] else "私聊"
                kind = FACT_KIND_MAP.get(kind_label, "profile")
                if kind_label not in FACT_KIND_MAP:
                    result["warnings"].append(
                        f"未知类别「{kind_label}」，按“其他/背景”处理"
                    )
                result["facts"].append(
                    (kind, content, "owner_public" if scope == "公开" else "owner_private")
                )
            else:
                if len(cells) < 1 or not cells[0]:
                    continue
                when = cells[1] if len(cells) > 1 and cells[1] else ""
                result["commitments"].append((cells[0], when))
            continue

        if "=" in line:
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip()
            if not value:
                continue
            if section == "称呼":
                if key in ("私聊称呼", "称呼"):
                    result["address"]["私聊称呼"] = value
                elif key in ("群内称呼", "群里称呼"):
                    result["address"]["群内称呼"] = value
                else:
                    result["address"][key] = value
            elif section == "关系":
                mapping = RELATION_KEYS.get(key)
                if mapping:
                    result["relation"][mapping] = value
                else:
                    result["relation"][("relation", key)] = value
            elif section in ("核心", "核心设定"):
                result["relation"][("relation", key)] = value
            else:
                result["warnings"].append(f"“{key} = {value}”在未知区块「{section}」里，已忽略")
            continue

        # 兼容原模板的 “- 项目：值”
        if line.startswith("-") and ("：" in line or ":" in line):
            body = line.lstrip("-").strip()
            sep = "：" if "：" in body else ":"
            key, value = body.split(sep, 1)
            key, value = key.strip(), value.strip()
            if not value or set(value) <= set("_ -"):
                continue
            mapping = RELATION_KEYS.get(key)
            if mapping:
                result["relation"][mapping] = value
            elif key in ("私聊希望她怎么称呼你", "私聊称呼"):
                result["address"]["私聊称呼"] = value
            elif key in ("群里希望她怎么称呼你", "群内称呼"):
                result["address"]["群内称呼"] = value
            else:
                result["warnings"].append(f"未识别的行，已忽略：{line}")
    return result


def print_plan(plan: dict) -> None:
    out("—— 导入预览 ——")
    if plan["address"]:
        out("[称呼]")
        for key, value in plan["address"].items():
            out(f"  {key}：{value}")
    if plan["relation"]:
        out("[核心]")
        for (section, title), value in plan["relation"].items():
            out(f"  {CORE_SECTIONS.get(section, section)}/{title}：{value}")
    if plan["facts"]:
        out("[核心事实]")
        for kind, content, scope in plan["facts"]:
            out(f"  {kind}（{scope}）：{content}")
    if plan["commitments"]:
        out("[约定]")
        for content, when in plan["commitments"]:
            out(f"  {content}" + (f"（{when}）" if when else ""))
    if plan["warnings"]:
        out("[提醒]")
        for w in plan["warnings"]:
            out(f"  - {w}")
    total = len(plan["relation"]) + len(plan["facts"]) + len(plan["commitments"])
    out(f"合计 {total} 条可写入。")


# --------------------------------------------------------------------------- #
# 导入
# --------------------------------------------------------------------------- #

def cmd_import(args) -> int:
    path = Path(args.file) if args.file else DEFAULT_TEMPLATE
    if not path.exists():
        out(f"找不到文件：{path}")
        return 2
    plan = parse_profile(path.read_text(encoding="utf-8-sig"))
    print_plan(plan)
    if not (plan["address"] or plan["relation"] or plan["facts"] or plan["commitments"]):
        out("没有解析到任何内容，检查格式。")
        return 2
    if not args.apply:
        out("")
        out("这是预览，没有写入。确认后加 --apply。")
        return 0

    store = MemoryStore(args.db)
    owners = store.list_owners()
    if not owners:
        out("警告：还没有绑定主人。档案会以 owner_id=primary 保存，但主人身份仍不生效。")
        out("先运行：python tools\\feiyu_memory_admin.py bind-owner --user-id <QQ号>")
    owner_id = owners[0]["owner_id"] if owners else "primary"

    written = 0
    core_scope = getattr(args, "core_scope", "owner_private")
    for title, value in plan["address"].items():
        store.set_core(owner_id, "address", value, title=title, scope_type=core_scope)
        written += 1
    if owners and plan["address"]:
        owner = owners[0]
        store.bind_owner(
            owner["platform"],
            owner["user_id"],
            bot_instance=owner["bot_instance"],
            owner_id=owner["owner_id"],
            display_name=owner["display_name"],
            owner_label=plan["address"].get("私聊称呼") or owner["owner_label"],
            group_label=plan["address"].get("群内称呼") or owner["group_label"],
            relation=owner["relation"],
            intimacy=owner["intimacy"],
            taboo=owner["taboo"],
            comfort=owner["comfort"],
            public_mode=bool(owner["public_mode"]),
        )
    for (section, title), value in plan["relation"].items():
        store.set_core(owner_id, section, value, title=title, scope_type=core_scope)
        written += 1

    existing = store.list_records(owner_id=owner_id, limit=500)
    seen = {(r["content"], r["kind"], r["scope_type"]) for r in existing}

    for kind, content, scope in plan["facts"]:
        if (content, kind, scope) in seen:
            out(f"跳过（已存在）：{compact(content, 40)}")
            continue
        rec = store.add_record(
            owner_id=owner_id,
            kind=kind,
            content=content,
            epistemic_type="owner_asserted",
            scope_type=scope,
            importance=3,
            created_by="import",
            evidence=[{
                "kind": "profile_file",
                "ref": str(path.name),
                "excerpt": compact(content, 200),
            }],
        )
        seen.add((content, kind, scope))
        written += 1
        out(f"  + {rec['id']} {compact(content, 50)}")

    for content, when in plan["commitments"]:
        text = content if not when else f"{content}（时间要求：{when}）"
        if (text, "commitment", "owner_private") in seen:
            out(f"跳过（已存在）：{compact(text, 40)}")
            continue
        rec = store.add_record(
            owner_id=owner_id,
            kind="commitment",
            content=text,
            epistemic_type="owner_asserted",
            scope_type="owner_private",
            importance=4,
            created_by="import",
            evidence=[{
                "kind": "profile_file",
                "ref": str(path.name),
                "excerpt": compact(text, 200),
            }],
        )
        seen.add((text, "commitment", "owner_private"))
        written += 1
        out(f"  + {rec['id']} {compact(text, 50)}")

    store.audit("import_profile", actor="cli",
                detail=f"{path.name} written={written}")
    out("")
    out(f"写入完成：{written} 条。数据库：{store.db_path}")
    store.close()
    return 0


# --------------------------------------------------------------------------- #
# 绑定与核心
# --------------------------------------------------------------------------- #

def cmd_bind(args) -> int:
    store = MemoryStore(args.db)
    owner = store.bind_owner(
        args.platform,
        args.user_id,
        bot_instance=args.bot_instance,
        owner_label=args.label,
        group_label=args.group_label,
        relation=args.relation,
        intimacy=args.intimacy,
        taboo=args.taboo,
        comfort=args.comfort,
        public_mode=args.public,
        display_name=args.display_name,
    )
    out("已绑定主人：")
    out(f"  平台：{owner['platform']}")
    out(f"  账号：{mask_uid(owner['user_id'])}（完整值已写入本地库）")
    out(f"  机器人实例：{owner['bot_instance']}")
    out(f"  私聊称呼：{owner['owner_label']}；群内称呼：{owner['group_label'] or '（未设置）'}")
    out(f"  数据库：{store.db_path}")
    out("")
    out("提醒：这个绑定只认这个平台账号。聊天里自称主人无效。")
    store.close()
    return 0


def cmd_unbind(args) -> int:
    store = MemoryStore(args.db)
    ok = store.unbind_owner(args.platform, args.user_id, args.bot_instance)
    out("已解除绑定。" if ok else "没有找到这条绑定。")
    store.close()
    return 0 if ok else 1


def cmd_owners(args) -> int:
    store = MemoryStore(args.db)
    owners = store.list_owners()
    if not owners:
        out("还没有绑定主人。")
    for o in owners:
        out(
            f"{o['platform']}/{o['bot_instance']} 账号 {mask_uid(o['user_id'])} "
            f"称呼={o['owner_label']} 群称呼={o['group_label'] or '-'} "
            f"公开身份={'是' if o['public_mode'] else '否'} 更新={o['updated_at']}"
        )
    store.close()
    return 0


def cmd_set_core(args) -> int:
    store = MemoryStore(args.db)
    owner_id = args.owner_id
    if not store.list_owners() and owner_id == "primary":
        out("提示：尚无主人绑定，核心设定会挂到 owner_id=primary。")
    row = store.set_core(
        owner_id, args.section, args.content,
        title=args.title, scope_type=args.scope, priority=args.priority,
    )
    out(f"已写入核心：{args.section}/{args.title or '-'}（{row['scope_type']}）")
    store.close()
    return 0


def cmd_core_list(args) -> int:
    store = MemoryStore(args.db)
    rows = store.list_core(args.owner_id)
    if not rows:
        out("核心设定是空的。")
    for r in rows:
        out(
            f"[{r['section']}/{r['title'] or '-'}] ({r['scope_type']}) "
            f"{r['content']}"
        )
    store.close()
    return 0


# --------------------------------------------------------------------------- #
# 记录
# --------------------------------------------------------------------------- #

def cmd_add(args) -> int:
    store = MemoryStore(args.db)
    owner_id = args.owner_id
    evidence = []
    if args.evidence:
        evidence.append({"kind": "owner_note", "ref": "cli", "excerpt": args.evidence})
    if args.evidence_file:
        evidence.append({"kind": "file", "ref": args.evidence_file, "excerpt": ""})
    epistemic = args.epistemic
    if epistemic == "observed_dialogue" and not evidence:
        out("observed_dialogue 必须带 --evidence 或 --evidence-file。")
        return 2
    rec = store.add_record(
        owner_id=owner_id,
        kind=args.kind,
        content=args.content,
        epistemic_type=epistemic,
        scope_type=args.scope,
        scope_id=args.scope_id or "",
        fact_key=args.fact_key or "",
        importance=args.importance,
        occurred_at=args.occurred_at,
        expires_at=args.expires_at,
        evidence=evidence,
        created_by="cli",
    )
    out(f"已写入 {rec['id']}：{compact(rec['content'], 80)}")
    store.close()
    return 0


def cmd_list(args) -> int:
    store = MemoryStore(args.db)
    rows = store.list_records(
        owner_id=args.owner_id,
        status=args.status,
        kinds=args.kind,
        limit=args.limit,
    )
    if not rows:
        out("没有符合条件的记录。")
    for r in rows:
        tag = EPISTEMIC_TAG.get(r["epistemic_type"], "")
        out(
            f"{r['id']} [{r['status']}] {r['kind']} {r['scope_type']}:{r['scope_id'] or '-'} "
            f"{tag}{compact(r['content'], 70)}"
        )
    store.close()
    return 0


def cmd_show(args) -> int:
    store = MemoryStore(args.db)
    rec = store.get_record(args.id)
    if not rec:
        out("没有这条记录。")
        return 1
    out(json.dumps(rec, ensure_ascii=False, indent=2))
    store.close()
    return 0


def cmd_correct(args) -> int:
    store = MemoryStore(args.db)
    rec = store.correct_record(args.id, args.content, actor="cli")
    out(f"已纠正为 {rec['id']}（旧记录 {args.id} 失效）")
    store.close()
    return 0


def cmd_forget(args) -> int:
    store = MemoryStore(args.db)
    if not args.yes:
        out("会写入删除墓碑；确认加 --yes。")
        return 2
    ok = store.forget_record(args.id, reason=args.reason or "CLI 忘记", actor="cli")
    out(f"已忘记 {args.id}（恢复备份也不会复活）" if ok else "没有这条记录。")
    store.close()
    return 0 if ok else 1


def cmd_pause(args) -> int:
    store = MemoryStore(args.db)
    store.set_control("capture_enabled", "0" if args.on else "1", actor="cli")
    out("记录已暂停。" if args.on else "记录已恢复。")
    store.close()
    return 0


# --------------------------------------------------------------------------- #
# 状态 / 导出 / 备份
# --------------------------------------------------------------------------- #

def cmd_status(args) -> int:
    store = MemoryStore(args.db)
    s = store.stats()
    out("数据库：{}".format(s["db_path"]))
    out("主人绑定：{} 个".format(s["owners"]))
    out("核心设定：{} 条".format(s["core"]))
    out(
        "记忆记录：可用 {active} / 已取代 {superseded} / 已删除 {deleted}".format(**s)
    )
    out("证据：{} 条；向量：{} 条；墓碑：{} 条".format(
        s["evidence"], s["embeddings"], s["tombstones"]))
    out("记录开关：{}；删除纪元：{}".format(
        "开" if s["capture"] else "暂停", s["deletion_epoch"]))
    store.close()
    return 0


def cmd_export(args) -> int:
    store = MemoryStore(args.db)
    path = store.export_json(args.out or (PROJECT_ROOT / "memory" / "feiyu_export.json"))
    out(f"已导出：{path}")
    store.close()
    return 0


def cmd_backup(args) -> int:
    store = MemoryStore(args.db)
    path = store.backup(args.out or (store.db_path.parent / "backups"))
    out(f"已备份：{path}")
    store.close()
    return 0


def cmd_restore(args) -> int:
    if not args.yes:
        out("恢复会覆盖当前数据库；先停掉 AstrBot，确认后加 --yes。")
        return 2
    store = MemoryStore(args.db)
    result = store.restore(args.source)
    out(
        f"已恢复自 {result['source']}；墓碑 {result['tombstones']} 条，"
        f"其中 {result['reapplied']} 条记录被重新标记为已删除。"
    )
    store.close()
    return 0


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default=str(default_db_path()), help="数据库路径")

    parser = argparse.ArgumentParser(
        description="大肥鱼长期记忆本地管理",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("template", help="生成待填档案")
    p.add_argument("--out", default="")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_template)

    p = sub.add_parser("import", help="导入填写好的档案")
    p.add_argument("file", nargs="?", default="", help="默认读 主人档案填写.md")
    p.add_argument("--apply", action="store_true", help="真正写入（默认只预览）")
    p.add_argument(
        "--core-scope", default="owner_private",
        choices=["owner_private", "owner_public"],
        help="称呼/关系这类核心设定的可见范围；允许进群使用就选 owner_public",
    )
    p.add_argument("--db", default=str(default_db_path()))
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("bind-owner", help="绑定主人账号", parents=[common])
    p.add_argument("--platform", default="aiocqhttp")
    p.add_argument("--user-id", required=True)
    p.add_argument("--bot-instance", default="*")
    p.add_argument("--label", default="主人")
    p.add_argument("--group-label", default="")
    p.add_argument("--relation", default="")
    p.add_argument("--intimacy", default="")
    p.add_argument("--taboo", default="")
    p.add_argument("--comfort", default="")
    p.add_argument("--display-name", default="")
    p.add_argument("--public", action="store_true")
    p.set_defaults(func=cmd_bind)

    p = sub.add_parser("unbind-owner", help="解除主人绑定", parents=[common])
    p.add_argument("--platform", default="aiocqhttp")
    p.add_argument("--user-id", required=True)
    p.add_argument("--bot-instance", default="*")
    p.set_defaults(func=cmd_unbind)

    p = sub.add_parser("owners", help="列出主人绑定", parents=[common])
    p.set_defaults(func=cmd_owners)

    p = sub.add_parser("set-core", help="写一条核心设定", parents=[common])
    p.add_argument("--owner-id", default="primary")
    p.add_argument("--section", required=True, choices=list(CORE_SECTIONS))
    p.add_argument("--title", default="")
    p.add_argument("--content", required=True)
    p.add_argument("--scope", default="owner_private",
                   choices=["owner_private", "owner_public"])
    p.add_argument("--priority", type=int, default=50)
    p.set_defaults(func=cmd_set_core)

    p = sub.add_parser("core", help="查看核心设定", parents=[common])
    p.add_argument("--owner-id", default="primary")
    p.set_defaults(func=cmd_core_list)

    p = sub.add_parser("add", help="新增一条记忆", parents=[common])
    p.add_argument("--owner-id", default="primary")
    p.add_argument("--kind", required=True, choices=list(KINDS))
    p.add_argument("--content", required=True)
    p.add_argument("--epistemic", default="owner_asserted",
                   choices=["seed_lore", "owner_asserted", "observed_dialogue"])
    p.add_argument("--scope", default="owner_private",
                   choices=["owner_private", "owner_public", "group", "member"])
    p.add_argument("--scope-id", default="")
    p.add_argument("--fact-key", default="")
    p.add_argument("--importance", type=int, default=3)
    p.add_argument("--occurred-at", default=None)
    p.add_argument("--expires-at", default=None)
    p.add_argument("--evidence", default="")
    p.add_argument("--evidence-file", default="")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("list", help="列出记录", parents=[common])
    p.add_argument("--owner-id", default=None)
    p.add_argument("--status", default="active")
    p.add_argument("--kind", action="append", default=None)
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("show", help="查看一条记录", parents=[common])
    p.add_argument("id")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("correct", help="纠正一条记录", parents=[common])
    p.add_argument("id")
    p.add_argument("content")
    p.set_defaults(func=cmd_correct)

    p = sub.add_parser("forget", help="忘记一条记录", parents=[common])
    p.add_argument("id")
    p.add_argument("--reason", default="")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_forget)

    p = sub.add_parser("pause", help="暂停记录", parents=[common])
    p.add_argument("--on", action="store_true", help="暂停（默认是恢复）")
    p.set_defaults(func=cmd_pause)

    p = sub.add_parser("status", help="状态", parents=[common])
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("export", help="导出 JSON", parents=[common])
    p.add_argument("--out", default="")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("backup", help="备份数据库", parents=[common])
    p.add_argument("--out", default="")
    p.set_defaults(func=cmd_backup)

    p = sub.add_parser("restore", help="从备份恢复", parents=[common])
    p.add_argument("source")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_restore)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 1
    try:
        return args.func(args)
    except MemoryError as exc:
        out(f"没成：{exc}")
        return 2
    except FileNotFoundError as exc:
        out(f"找不到：{exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

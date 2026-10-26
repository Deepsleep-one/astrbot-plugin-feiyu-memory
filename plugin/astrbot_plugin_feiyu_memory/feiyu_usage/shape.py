"""Request-shape profiling: where the input tokens of one call physically sit.

Only sizes and counts are produced - never prompt text, message bodies or tool
names beyond a numeric total. Callers may persist the returned mapping as-is.

Characters are exact; the token figure is a rough local estimate because the
real tokenizer lives on the provider side. Treat it as a ratio, not a bill.
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Mapping

CJK_DIVISOR = 1.6
LATIN_DIVISOR = 3.6


def estimate_tokens(text: str) -> int:
    """Crude token estimate: CJK characters cost noticeably more than Latin ones."""
    if not text:
        return 0
    wide = 0
    for char in text:
        if "\u2e80" <= char <= "\u9fff" or "\uff00" <= char <= "\uffef":
            wide += 1
    narrow = len(text) - wide
    return int(wide / CJK_DIVISOR + narrow / LATIN_DIVISOR) + 1


def _field(obj: Any, name: str, default: Any = None) -> Any:
    """Read a field from either a mapping or an object - requests carry both."""
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _single_part(part: Any) -> tuple[str, int]:
    """Return (text, image_count) for one content part (usually a mapping)."""
    if isinstance(part, Mapping) or not isinstance(part, (str, bytes, bytearray)):
        if part is None or isinstance(part, str):
            return ("" if part is None else str(part)), 0
        kind = str(_field(part, "type") or "")
        if kind in {"image_url", "image", "input_image"} or _field(part, "image_url") is not None:
            return "", 1
        text = _field(part, "text")
        return (str(text), 0) if text else ("", 0)
    return str(part), 0



def _parts_of(content: Any) -> tuple[str, int]:
    """Return (text, image_count) for one message content field."""
    if isinstance(content, str):
        return content, 0
    if isinstance(content, Mapping):
        return _single_part(content)
    texts: list[str] = []
    images = 0
    if isinstance(content, Iterable) and not isinstance(content, (bytes, bytearray)):
        for part in content:
            text, is_image = _single_part(part)
            if text:
                texts.append(text)
            images += is_image
    elif content is not None:
        texts.append(str(content))
    return "\n".join(texts), images


def _tool_payload(func_tool: Any) -> tuple[int, int]:
    """Return (tool_count, schema_chars) for a ToolSet-like object.

    AstrBot's ``FunctionTool`` is a dataclass over ``ToolSchema`` (name /
    description / parameters) and has no ``spec`` attribute, so the payload is
    rebuilt from those three fields when an explicit schema is absent.
    """
    if func_tool is None:
        return 0, 0
    specs = _field(func_tool, "tools")
    if specs is None:
        specs = _field(func_tool, "function_list")
    if specs is None:
        return 0, 0
    try:
        count = len(specs)
    except TypeError:
        return 0, 0
    payload = []
    active = 0
    for item in specs:
        if _field(item, "active", True) is False:
            continue
        active += 1
        spec = _field(item, "spec")
        if spec is None:
            spec = _field(item, "schema")
        if spec is None:
            spec = {
                "name": _field(item, "name"),
                "description": _field(item, "description"),
                "parameters": _field(item, "parameters"),
            }
        payload.append(spec)
    try:
        return active or count, len(json.dumps(payload, ensure_ascii=False, default=str))
    except Exception:  # noqa: BLE001 - 算不出来就当没有，绝不抛
        return active or count, 0


def describe_request(
    system_prompt: str | None = None,
    contexts: Iterable[Any] | None = None,
    func_tool: Any = None,
    *,
    extra_parts: Iterable[Any] | None = None,
) -> dict[str, Any]:
    """Summarise one provider request: sizes per section, no content."""
    system_text = str(system_prompt or "")

    roles: dict[str, dict[str, int]] = {}
    context_chars = 0
    context_images = 0
    context_messages = 0
    context_tokens = 0
    for message in contexts or ():
        role = str(_field(message, "role") or "unknown")
        content = _field(message, "content", message)
        text, images = _parts_of(content)
        context_messages += 1
        context_chars += len(text)
        context_images += images
        context_tokens += estimate_tokens(text)
        bucket = roles.setdefault(role, {"messages": 0, "chars": 0})
        bucket["messages"] += 1
        bucket["chars"] += len(text)

    extra_chars = 0
    extra_images = 0
    extra_tokens = 0
    for part in extra_parts or ():
        text, images = _parts_of(part)
        extra_chars += len(text)
        extra_images += images
        extra_tokens += estimate_tokens(text)

    tools_count, tools_chars = _tool_payload(func_tool)

    return {
        "system_chars": len(system_text),
        "context_messages": context_messages,
        "context_chars": context_chars,
        "context_images": context_images,
        "extra_chars": extra_chars,
        "extra_images": extra_images,
        "tools_count": tools_count,
        "tools_chars": tools_chars,
        "total_chars": len(system_text) + context_chars + extra_chars + tools_chars,
        "estimated_tokens": estimate_tokens(system_text)
        + context_tokens
        + extra_tokens
        + int(tools_chars / LATIN_DIVISOR),
        "roles": roles,
    }

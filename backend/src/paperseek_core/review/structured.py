"""Bounded structured output parsing and repair."""
from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from paperseek_core.errors import LLMRequestError
from paperseek_core.json_repair import close_brackets
from paperseek_core.ports import CancellationToken, LLMPort
from paperseek_core.review.prompts import FORMATTER, SYSTEM, SYSTEM_ZH, _say

T = TypeVar("T", bound=BaseModel)


class ReviewOutputError(ValueError):
    """The model did not produce a valid, grounded structure after one repair."""

    def __init__(self, message: str, result: BaseModel | None = None, *, task: str = "", reason: str = "",
                 output: str = "") -> None:
        super().__init__(message)
        self.result = result
        self.task, self.reason = task, reason
        self.output = output


def _clean_json(output: str) -> str:
    """Deterministic cleanup before spending a model call: fences, surrounding prose and missing closing brackets."""
    stripped = output.strip()
    # Take the whole fenced content (the closing fence may be missing too) before any repair, so a
    # reply short of its final brackets is not cut back to its last complete object.
    fence = re.search(r"```(?:json)?[ \t]*\n?(.*?)(?:```|$)", stripped, re.S)
    if fence and "{" in fence.group(1):
        stripped = fence.group(1).strip()
    start = stripped.find("{")
    if start < 0:
        return stripped
    whole = stripped[start:]
    end = whole.rfind("}")
    # The full text first; trailing prose after the last "}" second.
    for candidate in (whole, whole[:end + 1]) if end >= 0 else (whole,):
        closed = _closed(candidate)
        if _parses(closed):
            return closed
    return whole[:end + 1] if end >= 0 else whole


def _parses(text: str) -> bool:
    try:
        json.loads(text)
    except json.JSONDecodeError:
        return False
    return True


def _closed(text: str) -> str:
    # Some models end a complete reply without its final brackets; adding them avoids a repair call.
    try:
        json.loads(text)
        return text
    except json.JSONDecodeError:
        closed = close_brackets(text)
        try:
            json.loads(closed)
        except json.JSONDecodeError:
            return text
        return closed


def _syntax_only(exc: ValidationError) -> bool:
    return all(error["type"] == "json_invalid" for error in exc.errors())


async def _reformat(llm: LLMPort, cancellation: CancellationToken, contract: str, output: str, max_tokens: int) -> str:
    """A separate formatting pass: long prose often breaks JSON escaping while its content is fine."""
    try:
        formatted = await llm.chat_json(
            [{"role": "system", "content": FORMATTER + "\nSCHEMA:\n" + contract}, {"role": "user", "content": output}],
            temperature=0, max_tokens=max_tokens,
        )
    except LLMRequestError:
        return _clean_json(output)
    cancellation.raise_if_cancelled()
    return _clean_json(formatted)


async def structured_call(
    llm: LLMPort, cancellation: CancellationToken, task: str, instruction: str,
    data: dict, schema: type[T], *, max_tokens: int = 4096,
    validate: Callable[[T], object] | None = None, attempts: int = 2, language: str = "en",
) -> T:
    contract = json.dumps(schema.model_json_schema(), ensure_ascii=False, separators=(",", ":"))
    messages = [
        {"role": "system", "content": _say(language, SYSTEM, SYSTEM_ZH) + f"\nTASK: {task}\n" + instruction
         + _say(language,
                "\nOUTPUT CONTRACT: Return one JSON object conforming to this schema. "
                "Use the exact field names, types, enum values and array/string limits; no extra keys. "
                "Identifiers must be copied from the input, not translated or shortened.\n",
                "\n输出约定：返回一个符合以下 JSON Schema 的 JSON 对象。字段名、类型、枚举值和数组/字符串长度限制"
                "必须完全一致，不要添加多余字段。所有标识符必须从输入原样复制，不得翻译或缩写。\n") + contract},
        {"role": "user", "content": json.dumps(data, ensure_ascii=False)},
    ]
    for attempt in range(attempts):
        cancellation.raise_if_cancelled()
        output = await llm.chat_json(messages, temperature=0, max_tokens=max_tokens)
        cancellation.raise_if_cancelled()
        result = None
        cleaned = _clean_json(output)
        try:
            try:
                result = schema.model_validate_json(cleaned)
            except ValidationError as exc:
                # Broken JSON syntax is a formatting problem: fix it without regenerating content.
                if not _syntax_only(exc):
                    raise
                cleaned = await _reformat(llm, cancellation, contract, output, max_tokens)
                result = schema.model_validate_json(cleaned)
            if validate:
                validate(result)
            return result
        except ValueError as exc:
            reason = "\n".join(
                f"{'.'.join(str(part) for part in error['loc']) or '$'}: {error['msg']}"
                for error in exc.errors(include_input=False, include_context=False, include_url=False)[:8]
            ) if isinstance(exc, ValidationError) else str(exc)
            reason = reason[:2000]
            if attempt == attempts - 1:
                raise ReviewOutputError(f"Invalid {task} output.", result, task=task, reason=reason, output=cleaned) from exc
            messages.extend([
                {"role": "assistant", "content": output},
                {"role": "user", "content": _say(
                    language,
                    "Repair the specific errors below and return the COMPLETE JSON object. "
                    "Preserve valid content; do not return a patch or explanation.\n" + reason
                    + "\nFollow the output contract and cross-field rules in the system task.",
                    "请修正下列具体错误，并返回完整的 JSON 对象。保留有效内容，不要只返回补丁或解释。\n" + reason
                    + "\n遵守系统任务中的输出约定和跨字段规则。")},
            ])
    raise AssertionError("Unreachable bounded generation state")

"""
analyzer.py —— 调用 Claude Sonnet 4.6 对聊天内容做翻译 + 风险评估

战斗内 / 战斗外两个场景使用不同的 system prompt（见 prompts.py），
两套提示各自独立 prompt cache，5 分钟内同场景的所有批次复用缓存。
模型输出 TSV（5 列：idx / translation / risk_level / risk_type / risk_reason），
比 JSON 更不容易因为内嵌引号导致解析失败。

对外暴露：
    analyze_in_battle(messages, progress_callback=None, country=None) -> list[dict]
    analyze_out_battle(messages, progress_callback=None, country=None) -> list[dict]
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Callable, Optional

from anthropic import Anthropic
from dotenv import load_dotenv

from prompts import IN_BATTLE_SYSTEM, OUT_BATTLE_SYSTEM, language_hint

# 兼容两种 .env 放置（同 db.py）
for _env_path in [Path(__file__).parent / '.env',
                  Path(__file__).parent.parent / '.env']:
    if _env_path.exists():
        load_dotenv(_env_path)
        break

MODEL      = "claude-sonnet-4-6"  # Sonnet 对小语种和绕过屏蔽识别更准
BATCH_SIZE = 30   # 每批送 30 条聊天

_client: Optional[Anthropic] = None


def _get_client() -> Anthropic:
    """惰性创建 Anthropic 客户端（避免无 KEY 时 import 报错）。"""
    global _client
    if _client is None:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError(
                "ANTHROPIC_API_KEY 未在 .env 中配置，请补全后重试。"
            )
        _client = Anthropic()
    return _client


def _strip_code_fence(text: str) -> str:
    """模型偶尔仍会用 ``` 包裹，去掉外层。"""
    text = text.strip()
    if text.startswith("```"):
        nl = text.find("\n")
        if nl > 0:
            text = text[nl + 1:]
        if text.endswith("```"):
            text = text[:-3].rstrip()
    return text


_VALID_RISK_TYPES = {
    "normal", "abuse", "discrimination", "threat", "other",
    "ad", "sexual", "scam",
}


def _parse_tsv_line(line: str) -> dict | None:
    """
    解析单行 TSV：idx<TAB>translation<TAB>risk_level<TAB>risk_type<TAB>risk_reason
    解析失败返回 None。
    """
    line = line.strip()
    if not line:
        return None
    parts = line.split("\t")
    if len(parts) < 5:
        # 模型偶尔会用多个空格代替 tab — 兜底用 maxsplit
        parts = line.split(None, 4)
    if len(parts) < 5:
        return None
    try:
        idx = int(parts[0].strip())
    except ValueError:
        return None
    translation = parts[1].strip()
    try:
        risk_level = int(parts[2].strip())
    except ValueError:
        risk_level = 0
    risk_type = parts[3].strip().lower()
    if risk_type not in _VALID_RISK_TYPES:
        risk_type = "other"
    risk_reason = parts[4].strip()
    return {
        "idx":         idx,
        "translation": translation,
        "risk_level":  max(0, min(3, risk_level)),
        "risk_type":   risk_type,
        "risk_reason": risk_reason,
    }


def _analyze_batch(messages: list[str], system_prompt: str,
                   country: str | None = None) -> list[dict]:
    """
    送一个批次给模型，按 idx 对齐回结果。
    模型偶尔漏一条或多一条时也能稳定返回与输入等长的列表。
    country 不为空时在 user prompt 前缀加入语言提示，system prompt 保持不变以命中 cache。
    """
    client = _get_client()

    user_payload = json.dumps(
        [{"idx": i, "content": msg} for i, msg in enumerate(messages)],
        ensure_ascii=False,
    )
    user_text = language_hint(country) + user_payload

    response = client.messages.create(
        model      = MODEL,
        max_tokens = 8000,
        system     = [{
            "type":          "text",
            "text":          system_prompt,
            "cache_control": {"type": "ephemeral"},
        }],
        messages = [{"role": "user", "content": user_text}],
    )

    text = next(b.text for b in response.content if b.type == "text")
    text = _strip_code_fence(text)

    by_idx: dict[int, dict] = {}
    for line in text.splitlines():
        parsed = _parse_tsv_line(line)
        if parsed is None:
            continue
        by_idx[parsed["idx"]] = parsed

    aligned: list[dict] = []
    for i, msg in enumerate(messages):
        r = by_idx.get(i)
        if r is None:
            aligned.append({
                "idx":         i,
                "translation": msg,
                "risk_level":  0,
                "risk_type":   "normal",
                "risk_reason": "(模型未返回该条结果)",
            })
        else:
            aligned.append({**r, "idx": i})

    return aligned


def _analyze_messages(
    messages: list[str],
    system_prompt: str,
    progress_callback: Optional[Callable[[int, int], None]] = None,
    country: Optional[str] = None,
) -> list[dict]:
    """通用批次循环：把 messages 切成 BATCH_SIZE 一批，逐批调用模型。"""
    if not messages:
        return []

    results: list[dict] = []
    total_batches = (len(messages) + BATCH_SIZE - 1) // BATCH_SIZE

    for batch_idx in range(total_batches):
        start = batch_idx * BATCH_SIZE
        end   = min(start + BATCH_SIZE, len(messages))
        batch = messages[start:end]

        if progress_callback:
            progress_callback(batch_idx + 1, total_batches)

        batch_result = _analyze_batch(batch, system_prompt, country=country)
        # 把批次内 idx 映射回全局 idx
        for r in batch_result:
            r["idx"] = r["idx"] + start
        results.extend(batch_result)

    return results


# ============================================================
# 对外接口
# ============================================================

def analyze_in_battle(
    messages: list[str],
    progress_callback: Optional[Callable[[int, int], None]] = None,
    country: Optional[str] = None,
) -> list[dict]:
    """
    分析【战斗内】聊天。
    country 为玩家所在国家二字母代码（如 "TR"），用于翻译时的语种提示。
    返回与 messages 一一对应的 dict 列表，每项含：
        idx / translation / risk_level / risk_type / risk_reason
    """
    return _analyze_messages(messages, IN_BATTLE_SYSTEM, progress_callback, country)


def analyze_out_battle(
    messages: list[str],
    progress_callback: Optional[Callable[[int, int], None]] = None,
    country: Optional[str] = None,
) -> list[dict]:
    """
    分析【战斗外】聊天。
    country 为玩家所在国家二字母代码（如 "TR"），用于翻译时的语种提示。
    """
    return _analyze_messages(messages, OUT_BATTLE_SYSTEM, progress_callback, country)

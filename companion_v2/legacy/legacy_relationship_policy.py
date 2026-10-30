"""Pure v6.5.6 rules from menglimi/astrbot_plugin_private_companion.

Extracted from relationship_policy.py without changing executable behavior.
Source SHA256: 70a87a0faeed65ec7847ad475cd1e457b64fafffbd21a4622f10ae3ceef25981.
"""

from __future__ import annotations

import json

import re

from copy import deepcopy

from typing import Any

RELATIONSHIP_SCORE_MIN = -1200

RELATIONSHIP_SCORE_MAX = 1200

RELATIONSHIP_STAGE_PROVIDER_ROUTE_KEYS = (
    "deeply_distant",
    "strongly_distant",
    "distant",
    "acquaintance",
    "familiar",
    "close",
    "intimate",
    "deeply_bonded",
    "owner_exclusive",
)

_PROVIDER_ROUTE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

_DEFAULT_STAGES: tuple[dict[str, Any], ...] = (
    {
        "key": "deeply_distant",
        "label": "极度疏离",
        "min": -1200,
        "max": -801,
        "description": "明显保持距离，只做必要、克制且尊重边界的回应。",
        "tone": "克制、礼貌、低压力，不主动拉近关系",
        "address_level": "仅使用姓名、固定称呼或“你”",
        "proactive_care_limit": 0,
        "allow_playful_jokes": False,
        "allow_followup": False,
        "allow_memory_mention": False,
        "allow_daily_care": False,
    },
    {
        "key": "strongly_distant",
        "label": "强烈疏离",
        "min": -800,
        "max": -401,
        "description": "关系处于明显降温期，优先稳住边界和基本礼貌。",
        "tone": "平静、简短、不过度解释",
        "address_level": "只使用中性称呼",
        "proactive_care_limit": 0,
        "allow_playful_jokes": False,
        "allow_followup": False,
        "allow_memory_mention": False,
        "allow_daily_care": False,
    },
    {
        "key": "distant",
        "label": "疏离",
        "min": -400,
        "max": -1,
        "description": "保持自然但不过分熟络，避免擅自使用亲昵称呼。",
        "tone": "自然、客气、留有空间",
        "address_level": "中性称呼",
        "proactive_care_limit": 0,
        "allow_playful_jokes": False,
        "allow_followup": False,
        "allow_memory_mention": False,
        "allow_daily_care": False,
    },
    {
        "key": "acquaintance",
        "label": "初识",
        "min": 0,
        "max": 199,
        "description": "刚开始相处，友好回应并逐步观察偏好。",
        "tone": "友好、自然、不自来熟",
        "address_level": "优先固定称呼、姓名或“你”",
        "proactive_care_limit": 0,
        "allow_playful_jokes": False,
        "allow_followup": True,
        "allow_memory_mention": False,
        "allow_daily_care": False,
    },
    {
        "key": "familiar",
        "label": "熟悉",
        "min": 200,
        "max": 599,
        "description": "已有稳定互动，可以更轻松地接话和续话。",
        "tone": "轻松、友好、带一点熟悉感",
        "address_level": "可自然使用昵称",
        "proactive_care_limit": 1,
        "allow_playful_jokes": True,
        "allow_followup": True,
        "allow_memory_mention": True,
        "allow_daily_care": True,
    },
    {
        "key": "close",
        "label": "亲近",
        "min": 600,
        "max": 899,
        "description": "关系稳定亲近，可更自然地关心近况和提及共同经历。",
        "tone": "温暖、亲近、尊重当前节奏",
        "address_level": "可使用已确认的亲昵称呼",
        "proactive_care_limit": 2,
        "allow_playful_jokes": True,
        "allow_followup": True,
        "allow_memory_mention": True,
        "allow_daily_care": True,
    },
    {
        "key": "intimate",
        "label": "亲密",
        "min": 900,
        "max": 1199,
        "description": "关系亲密，表达可以柔软、有默契，但仍服从用户边界。",
        "tone": "亲密、柔软、不过度黏人",
        "address_level": "可使用双方已接受的亲昵称呼",
        "proactive_care_limit": 3,
        "allow_playful_jokes": True,
        "allow_followup": True,
        "allow_memory_mention": True,
        "allow_daily_care": True,
    },
    {
        "key": "deeply_bonded",
        "label": "深度联结",
        "min": 1200,
        "max": 1200,
        "description": "长期关系达到当前上限，以稳定默契为主，不继续扩大权限。",
        "tone": "默契、温柔、稳定",
        "address_level": "使用双方明确认可的专属称呼",
        "proactive_care_limit": 4,
        "allow_playful_jokes": True,
        "allow_followup": True,
        "allow_memory_mention": True,
        "allow_daily_care": True,
    },
)

_TEXT_LIMITS = {
    "label": 20,
    "description": 160,
    "tone": 120,
    "address_level": 100,
}

_BOOL_FIELDS = (
    "allow_playful_jokes",
    "allow_followup",
    "allow_memory_mention",
    "allow_daily_care",
)


def _single_line(value: Any, limit: int) -> str:
    return " ".join(str(value or "").replace("\r", " ").replace("\n", " ").split())[
        :limit
    ]


def _decode_policy(value: Any) -> Any:
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            return json.loads(text)
        except (TypeError, ValueError):
            return []
    return value


def normalize_relationship_stage_policy(value: Any) -> list[dict[str, Any]]:
    """Normalize the original stage policy while preserving fixed score ranges.

    Args:
        value: Original JSON string, stage list or policy mapping.

    Returns:
        Fresh normalized stages with legacy defaults for missing values.
    """
    raw = _decode_policy(value)
    if isinstance(raw, dict):
        raw = raw.get("stages")
    if not isinstance(raw, list):
        raw = []
    raw_by_key = {
        str(item.get("key") or "").strip(): item
        for item in raw
        if isinstance(item, dict) and item.get("key")
    }
    normalized: list[dict[str, Any]] = []
    for default in _DEFAULT_STAGES:
        item = raw_by_key.get(default["key"], {})
        stage = deepcopy(default)
        for field, limit in _TEXT_LIMITS.items():
            text = _single_line(item.get(field), limit)
            if text:
                stage[field] = text
        try:
            stage["proactive_care_limit"] = max(
                0, min(30, int(item.get("proactive_care_limit")))
            )
        except (TypeError, ValueError):
            pass
        for field in _BOOL_FIELDS:
            if type(item.get(field)) is bool:
                stage[field] = item[field]
        normalized.append(stage)
    return normalized


def relationship_stage_for_score(
    value: Any,
    policy: Any = None,
    *,
    previous_stage_key: Any = "",
    hysteresis_margin: int = 20,
) -> dict[str, Any]:
    """Resolve the original relationship stage and optional hysteresis.

    Args:
        value: Legacy score before normalization.
        policy: Original stage policy.
        previous_stage_key: Previously displayed stage, when available.
        hysteresis_margin: Score margin required for changing displayed stage.

    Returns:
        Stage projection computed by the original v6.5.6 rules.
    """
    try:
        score = int(value)
    except (TypeError, ValueError):
        score = 0
    score = max(RELATIONSHIP_SCORE_MIN, min(RELATIONSHIP_SCORE_MAX, score))
    stages = normalize_relationship_stage_policy(policy)
    stage_index = 0
    for index, stage in enumerate(stages):
        if stage["min"] <= score <= stage["max"]:
            stage_index = index
            break
    previous_key = str(previous_stage_key or "").strip()
    previous_index = next(
        (index for index, item in enumerate(stages) if item["key"] == previous_key), -1
    )
    try:
        margin = max(0, min(200, int(hysteresis_margin or 0)))
    except (TypeError, ValueError):
        margin = 20
    if previous_index >= 0 and stage_index != previous_index:
        if stage_index > previous_index:
            enter_score = int(stages[previous_index + 1]["min"]) + margin
            if score < enter_score:
                stage_index = previous_index
        else:
            exit_score = int(stages[previous_index]["min"]) - margin
            if score > exit_score:
                stage_index = previous_index
    stage = deepcopy(stages[stage_index])
    span = max(0, int(stage["max"]) - int(stage["min"]))
    within = 1.0 if span == 0 else (score - int(stage["min"])) / span
    return {
        "value": score,
        "band": stage["label"],
        "phase": stage,
        "stages": stages,
        "stage_index": stage_index,
        "stage_progress": round(max(0.0, min(1.0, within)), 4),
        "min": RELATIONSHIP_SCORE_MIN,
        "max": RELATIONSHIP_SCORE_MAX,
        "trend": "unknown",
        "authority": "private_companion.relationship_score",
        "read_only": True,
        "schema_version": "chat.relationship_projection.v1",
    }

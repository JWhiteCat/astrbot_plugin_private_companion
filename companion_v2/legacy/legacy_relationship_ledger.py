"""Pure v6.5.6 rules from menglimi/astrbot_plugin_private_companion.

Extracted from relationship_ledger.py, with only imports and documentation adapted.
Source SHA256: f18862101c2687398eaee15adc5c59711367af0fd13c6a6d73cc9558ed737bfe.
"""

from __future__ import annotations

from copy import deepcopy

from datetime import datetime, timezone

from hashlib import sha256

from math import isfinite

from typing import Any

from zoneinfo import ZoneInfo

from .legacy_relationship_policy import (
    RELATIONSHIP_SCORE_MAX,
    RELATIONSHIP_SCORE_MIN,
    normalize_relationship_stage_policy,
    relationship_stage_for_score,
)

OWNER_EXCLUSIVE_MODE = "owner_exclusive"

NORMAL_RELATIONSHIP_MODE = "normal"

DEFAULT_POSITIVE_DAILY_CAP = 12

DEFAULT_EVENT_WINDOW_SECONDS = 30 * 60

_EVENT_REASON_ALIASES = {
    "fast_inbound": "inbound",
    "fast_proactive_reply": "proactive_reply",
    "fast_interaction_warmth": "interaction_warmth",
}

DEFAULT_DECAY_GRACE_DAYS = 3

RELATIONSHIP_SCORE_SCHEMA_VERSION = 2

_LEGACY_SCORE_ANCHORS: tuple[tuple[int, int], ...] = (
    (-120, -1200),
    (-80, -800),
    (-40, -400),
    (0, 0),
    (3, 200),
    (16, 600),
    (55, 900),
    (120, 1200),
)

RELATIONSHIP_POSITIVE_STAGE_CAP_KEYS = (
    "familiar",
    "close",
    "intimate",
    "deeply_bonded",
)

DEFAULT_RELATIONSHIP_POSITIVE_STAGE_CAP_KEY = "close"

_POSITIVE_STAGE_SCORE_MAX = {
    "familiar": 599,
    "close": 899,
    "intimate": 1199,
    "deeply_bonded": RELATIONSHIP_SCORE_MAX,
}


def normalize_relationship_mode(value: Any, role: Any) -> str:
    mode = str(value or "").strip().lower()
    normalized_role = str(role or "").strip().lower()
    if mode == OWNER_EXCLUSIVE_MODE and normalized_role == "owner":
        return OWNER_EXCLUSIVE_MODE
    return NORMAL_RELATIONSHIP_MODE


def is_owner_exclusive(user: Any) -> bool:
    if not isinstance(user, dict):
        return False
    return (
        normalize_relationship_mode(
            user.get("relationship_mode"), user.get("relationship_role")
        )
        == OWNER_EXCLUSIVE_MODE
    )


def is_owner_role(user: Any) -> bool:
    """Return whether this relationship record belongs to the primary user.

    Args:
        user: Original relationship record.

    Returns:
        Whether the record explicitly names the owner role.
    """
    if not isinstance(user, dict):
        return False
    return str(user.get("relationship_role") or "").strip().lower() == "owner"


def normalize_relationship_positive_stage_cap_key(value: Any) -> str:
    """Return the configured ordinary-user positive relationship ceiling.

    Args:
        value: Untrusted legacy stage key.

    Returns:
        Supported stage key, or the original default.
    """
    key = str(value or "").strip().lower()
    return (
        key
        if key in RELATIONSHIP_POSITIVE_STAGE_CAP_KEYS
        else DEFAULT_RELATIONSHIP_POSITIVE_STAGE_CAP_KEY
    )


def relationship_positive_score_cap(value: Any) -> int:
    return _POSITIVE_STAGE_SCORE_MAX[
        normalize_relationship_positive_stage_cap_key(value)
    ]


def _effective_positive_stage_cap_key(
    user: dict[str, Any], configured: Any = None
) -> str:
    if (
        configured is None
        and not str(user.get("relationship_positive_stage_cap_key") or "").strip()
    ):
        # Preserve the historical low-level API for legacy records and third-
        # party callers that never opted into staged caps.  New companion
        # records persist an explicit cap and production calls pass it again,
        # so ordinary users still use the configured ceiling.
        return "deeply_bonded"
    return normalize_relationship_positive_stage_cap_key(
        configured
        if configured is not None
        else user.get("relationship_positive_stage_cap_key")
    )


def apply_relationship_event(
    user: Any,
    delta: Any,
    *,
    reason_code: Any,
    now: Any = None,
    event_id: Any = None,
    positive_daily_cap: int = DEFAULT_POSITIVE_DAILY_CAP,
    event_window_seconds: int = DEFAULT_EVENT_WINDOW_SECONDS,
    positive_event_cap: int = 4,
    negative_event_cap: int = 12,
    positive_stage_cap_key: Any = None,
    timezone_name: Any = None,
) -> dict[str, Any]:
    """Settle one auditable relationship event into the bounded score.

    Args:
        user: Mutable legacy relationship record.
        delta: Requested score change.
        reason_code: Original producer category used for replay protection.
        now: Original event timestamp in seconds.
        event_id: Native event identity, shared across fast and normal paths.
        positive_daily_cap: Positive daily score limit.
        event_window_seconds: Legacy event-window configuration.
        positive_event_cap: Maximum positive change per event.
        negative_event_cap: Maximum negative magnitude per event.
        positive_stage_cap_key: Ordinary participant stage ceiling.
        timezone_name: Time zone for daily limits, falling back to UTC.

    Returns:
        Original settlement receipt; the supplied user record is updated in place.
    """
    if not isinstance(user, dict):
        return _result(False, "invalid_user")
    numeric_delta = _integer(delta)
    if numeric_delta is None or numeric_delta == 0:
        return _result(False, "invalid_delta")
    reason = _reason(reason_code)
    if not reason:
        return _result(False, "missing_reason")
    ts = _timestamp(now)
    role = str(user.get("relationship_role") or "").strip().lower()
    mode = normalize_relationship_mode(user.get("relationship_mode"), role)
    user["relationship_mode"] = mode
    if mode == OWNER_EXCLUSIVE_MODE:
        return _result(
            False,
            "owner_exclusive_frozen",
            score=_score(user.get("relationship_score")),
        )

    ledger = user.setdefault("relationship_ledger", [])
    if not isinstance(ledger, list):
        ledger = []
        user["relationship_ledger"] = ledger
    window_seconds = max(60, int(event_window_seconds or DEFAULT_EVENT_WINDOW_SECONDS))
    dedupe_key = _event_key(reason, ts, event_id, window_seconds)
    explicit_event_id = str(event_id or "").strip()
    canonical_reason = _EVENT_REASON_ALIASES.get(reason, reason)
    violation_recovery = canonical_reason == "relationship_violation_recovery"
    duplicate = any(
        isinstance(item, dict) and item.get("event_key") == dedupe_key
        for item in ledger[-200:]
    )
    if not explicit_event_id and not duplicate:
        for item in reversed(ledger[-200:]):
            if not isinstance(item, dict):
                continue
            item_reason = _EVENT_REASON_ALIASES.get(
                _reason(item.get("reason_code")), _reason(item.get("reason_code"))
            )
            item_ts = _finite_number(item.get("created_at"))
            if (
                item_reason == canonical_reason
                and item_ts is not None
                and abs(ts - item_ts) < window_seconds
            ):
                duplicate = True
                break
    if duplicate:
        return _result(
            False, "duplicate_event", score=_score(user.get("relationship_score"))
        )

    positive_cap = (
        60 if violation_recovery else max(1, min(30, int(positive_event_cap or 4)))
    )
    negative_cap = max(1, min(60, int(negative_event_cap or 12)))
    requested = max(-negative_cap, min(positive_cap, numeric_delta))
    before = _score(user.get("relationship_score"))
    if requested > 0 and before >= 900 and not violation_recovery:
        requested = max(1, (requested + 1) // 2)
    elif requested > 0 and before >= 600 and not violation_recovery:
        requested = max(1, (requested * 3 + 3) // 4)
    applied = requested
    day_key = _calendar_date(ts, timezone_name).isoformat()
    totals = user.setdefault("relationship_daily_totals", {})
    if not isinstance(totals, dict) or totals.get("day") != day_key:
        totals = {"day": day_key, "positive": 0, "negative": 0}
        user["relationship_daily_totals"] = totals
    if applied > 0 and not violation_recovery:
        raw_daily_cap = (
            DEFAULT_POSITIVE_DAILY_CAP
            if positive_daily_cap is None
            else positive_daily_cap
        )
        cap = max(0, min(120, int(raw_daily_cap)))
        remaining = max(0, cap - _bounded_int(totals.get("positive"), 0, 0, cap))
        applied = min(applied, remaining)
        if applied <= 0:
            return _result(
                False,
                "positive_daily_cap",
                score=_score(user.get("relationship_score")),
            )

    positive_score_cap = relationship_positive_score_cap(
        _effective_positive_stage_cap_key(user, positive_stage_cap_key)
    )
    after = max(RELATIONSHIP_SCORE_MIN, min(RELATIONSHIP_SCORE_MAX, before + applied))
    if applied > 0 and not is_owner_role(user):
        after = min(after, positive_score_cap)
    applied = after - before
    if applied == 0:
        code = (
            "positive_stage_cap"
            if requested > 0
            and before >= positive_score_cap
            and not is_owner_role(user)
            else "score_bound"
        )
        return _result(False, code, score=before)
    user["relationship_score"] = after
    if applied > 0:
        if not violation_recovery:
            totals["positive"] = (
                _bounded_int(totals.get("positive"), 0, 0, 120) + applied
            )
        user["relationship_last_effective_at"] = ts
    else:
        totals["negative"] = _bounded_int(totals.get("negative"), 0, -120, 0) + applied
    entry = {
        "event_key": dedupe_key,
        "reason_code": reason,
        "delta": applied,
        "score_before": before,
        "score_after": after,
        "created_at": ts,
    }
    ledger.append(entry)
    if len(ledger) > 200:
        del ledger[:-200]
    return _result(True, "applied", score=after, delta=applied, entry=deepcopy(entry))


def clamp_relationship_positive_stage_cap(
    user: Any,
    *,
    cap_key: Any = None,
    now: Any = None,
    reason_code: Any = "relationship_positive_stage_cap_runtime_clamp",
) -> dict[str, Any]:
    """Clamp a normal positive score and append one auditable runtime entry.

    Args:
        user: Mutable legacy relationship record.
        cap_key: Configured ordinary participant stage ceiling.
        now: Original event timestamp in seconds.
        reason_code: Audit category for a required clamp.

    Returns:
        Original clamp receipt, including owner exemptions.
    """
    if not isinstance(user, dict):
        return _result(False, "invalid_user")
    key = _effective_positive_stage_cap_key(user, cap_key)
    user["relationship_positive_stage_cap_key"] = key
    if is_owner_exclusive(user):
        return _result(
            False,
            "owner_exclusive_exempt",
            score=_score(user.get("relationship_score")),
        )
    if is_owner_role(user):
        return _result(
            False, "owner_role_exempt", score=_score(user.get("relationship_score"))
        )
    before = _score(user.get("relationship_score"))
    if before <= 0:
        return _result(False, "non_positive_score_exempt", score=before)
    maximum = relationship_positive_score_cap(key)
    if before <= maximum:
        return _result(False, "within_positive_stage_cap", score=before)
    after = maximum
    user["relationship_score"] = after
    return _append_cap_audit_entry(
        user,
        before=before,
        after=after,
        now=now,
        reason_code=reason_code,
        metadata={"cap_key": key, "cap_max": maximum},
    )


def apply_natural_relationship_decay(
    user: Any,
    *,
    now: Any = None,
    policy: Any = None,
    grace_days: int = DEFAULT_DECAY_GRACE_DAYS,
    early_rate: int = 2,
    middle_rate: int = 5,
    late_rate: int = 8,
    timezone_name: Any = None,
) -> dict[str, Any]:
    """Idempotently decay positive scores toward zero by inactive calendar day.

    Args:
        user: Mutable legacy relationship record, including last activity.
        now: Observation timestamp in seconds.
        policy: Original stage configuration for decay hysteresis.
        grace_days: Inactive calendar days exempt from decay.
        early_rate: Daily decay in the early interval.
        middle_rate: Daily decay in the middle interval.
        late_rate: Daily decay in the late interval.
        timezone_name: Time zone for calendar days, falling back to UTC.

    Returns:
        Original decay receipt; state and ledger are updated in place.
    """
    if not isinstance(user, dict):
        return _result(False, "invalid_user")
    ts = _timestamp(now)
    if is_owner_exclusive(user):
        return _result(
            False,
            "owner_exclusive_frozen",
            score=_score(user.get("relationship_score")),
        )
    score = _score(user.get("relationship_score"))
    if score <= 0:
        return _result(False, "non_positive_score", score=score)
    last_effective = _finite_number(user.get("relationship_last_effective_at"))
    if not last_effective:
        last_effective = _finite_number(user.get("last_activity_at")) or _finite_number(
            user.get("last_seen")
        )
    if not last_effective or ts <= last_effective:
        return _result(False, "no_inactive_window", score=score)

    today = _calendar_date(ts, timezone_name)
    last_day = _calendar_date(last_effective, timezone_name)
    inactive_days = max(0, (today - last_day).days)
    grace = max(0, min(30, int(grace_days or 0)))
    if inactive_days <= grace:
        return _result(False, "within_grace", score=score)
    settled_text = str(user.get("relationship_decay_settled_day") or "").strip()
    try:
        settled_day = (
            datetime.fromisoformat(settled_text).date() if settled_text else last_day
        )
    except ValueError:
        settled_day = last_day
    start_offset = max(grace + 1, (settled_day - last_day).days + 1)
    if start_offset > inactive_days:
        return _result(False, "already_settled", score=score)

    rates = (
        max(0, min(30, int(early_rate or 0))),
        max(0, min(30, int(middle_rate or 0))),
        max(0, min(30, int(late_rate or 0))),
    )
    requested = sum(
        _decay_for_inactive_day(day, rates=rates)
        for day in range(start_offset, inactive_days + 1)
    )
    if requested <= 0:
        user["relationship_decay_settled_day"] = today.isoformat()
        return _result(False, "no_decay", score=score)
    before_projection = relationship_stage_for_score(score, policy)
    candidate = max(0, score - requested)
    candidate = _apply_stage_hysteresis(
        user, score, candidate, ts, policy, before_projection
    )
    applied = score - candidate
    user["relationship_decay_settled_day"] = today.isoformat()
    if applied <= 0:
        return _result(False, "stage_hysteresis", score=score)
    user["relationship_score"] = candidate
    ledger = user.setdefault("relationship_ledger", [])
    if not isinstance(ledger, list):
        ledger = []
        user["relationship_ledger"] = ledger
    entry = {
        "event_key": f"natural_decay:{today.isoformat()}",
        "reason_code": "natural_decay",
        "delta": -applied,
        "score_before": score,
        "score_after": candidate,
        "created_at": ts,
        "inactive_days": inactive_days,
    }
    ledger.append(entry)
    if len(ledger) > 200:
        del ledger[:-200]
    return _result(
        True, "applied", score=candidate, delta=-applied, entry=deepcopy(entry)
    )


def _append_cap_audit_entry(
    user: dict[str, Any],
    *,
    before: int,
    after: int,
    now: Any,
    reason_code: Any,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    ts = _timestamp(now)
    reason = _reason(reason_code) or "relationship_positive_stage_cap_runtime_clamp"
    ledger = user.setdefault("relationship_ledger", [])
    if not isinstance(ledger, list):
        ledger = []
        user["relationship_ledger"] = ledger
    entry = {
        "event_key": _event_key(
            reason, ts, f"{reason}:{before}:{after}:{metadata}", 60
        ),
        "reason_code": reason,
        "delta": after - before,
        "score_before": before,
        "score_after": after,
        "created_at": ts,
        "source": "relationship_cap",
        **metadata,
    }
    ledger.append(entry)
    if len(ledger) > 200:
        del ledger[:-200]
    return _result(
        True, reason, score=after, delta=after - before, entry=deepcopy(entry)
    )


def _apply_stage_hysteresis(
    user: dict[str, Any],
    before: int,
    candidate: int,
    ts: float,
    policy: Any,
    before_projection: dict[str, Any],
) -> int:
    after_projection = relationship_stage_for_score(candidate, policy)
    before_index = int(before_projection.get("stage_index") or 0)
    after_index = int(after_projection.get("stage_index") or 0)
    if after_index >= before_index:
        return candidate
    stages = normalize_relationship_stage_policy(policy)
    last_drop = _finite_number(user.get("relationship_last_decay_stage_drop_at")) or 0.0
    if last_drop and ts - last_drop < 7 * 86400:
        current_min = int(stages[before_index]["min"])
        return max(candidate, current_min)
    immediate_index = max(0, before_index - 1)
    limited = max(candidate, int(stages[immediate_index]["min"]))
    if relationship_stage_for_score(limited, policy).get("stage_index") < before_index:
        user["relationship_last_decay_stage_drop_at"] = ts
    return limited


def _decay_for_inactive_day(
    day: int, *, rates: tuple[int, int, int] = (2, 5, 8)
) -> int:
    configured = ((7, rates[0]), (14, rates[1]), (10**9, rates[2]))
    for maximum, amount in configured:
        if day <= maximum:
            return amount
    return rates[2]


def _event_key(reason: str, ts: float, event_id: Any, window_seconds: int) -> str:
    explicit = str(event_id or "").strip()
    canonical_reason = _EVENT_REASON_ALIASES.get(reason, reason)
    source = (
        f"{explicit[:160]}:{canonical_reason}"
        if explicit
        else f"{canonical_reason}:{ts:.6f}"
    )
    return sha256(source.encode("utf-8", errors="ignore")).hexdigest()[:24]


def _calendar_date(ts: float, timezone_name: Any = None):
    name = str(timezone_name or "").strip()
    try:
        tz = ZoneInfo(name) if name else timezone.utc
    except (KeyError, ValueError):
        tz = timezone.utc
    return datetime.fromtimestamp(ts, tz=tz).date()


def _reason(value: Any) -> str:
    text = "_".join(str(value or "").strip().lower().split())
    return "".join(ch for ch in text if ch.isalnum() or ch in {"_", "-"})[:48]


def _score(value: Any) -> int:
    parsed = _integer(value)
    return max(
        RELATIONSHIP_SCORE_MIN,
        min(RELATIONSHIP_SCORE_MAX, parsed if parsed is not None else 0),
    )


def _integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and isfinite(value):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if isfinite(number) else None


def _timestamp(value: Any) -> float:
    number = _finite_number(value)
    return (
        number
        if number is not None and number > 0
        else datetime.now(tz=timezone.utc).timestamp()
    )


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    parsed = _integer(value)
    return max(minimum, min(maximum, parsed if parsed is not None else default))


def _result(
    changed: bool,
    code: str,
    *,
    score: int | None = None,
    delta: int = 0,
    entry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "changed": bool(changed),
        "code": code,
        "delta": int(delta),
    }
    if score is not None:
        result["score"] = int(score)
    if entry is not None:
        result["entry"] = entry
    return result

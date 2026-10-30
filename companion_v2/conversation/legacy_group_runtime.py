"""Explicit V1 group admission state; never a substitute for state evolution."""

from copy import deepcopy
import json
import random
import re

from companion_v2.execution.models import TaskError, digest, stamp
from .group_interjection import day_key
from .group_observation import GroupPresenceStore


KIND = "legacy_admission"
ROLE_PREFIX = "conversation.legacy_group_role.v1:"
SLEEP_PHASES = frozenset({"falling_asleep", "light_sleep", "sleeping_again", "woken"})


def owner_key(context):
    """Keep V1 quota shared across all member sessions of one group.

    Args:
        context: Authenticated group and role context.

    Returns:
        Stable route owner, independent of the current speaker and conversation.
    """
    scope = context.scope
    return digest(
        [
            "legacy-group-admission.v1",
            scope.installation_id,
            scope.platform,
            scope.bot_id,
            scope.account_id,
            scope.native_scope_id,
            context.role_binding.role.subject_id,
        ]
    )


def cycle_profile(state, *, enabled, today):
    """Project the V1 cycle multipliers from its actual state inputs.

    Args:
        state: V1 daily state or a verified projection of that state.
        enabled: Effective cycle simulation switch.
        today: Local calendar date used by the original runtime.

    Returns:
        Interval and probability multipliers, including the neutral phase.
    """
    neutral = (1.0, 1.0)
    if not enabled or str(state.get("date") or "") not in ("", today):
        return neutral
    text = " ".join(str(state.get("body_cycle") or "").split())[:100]
    if not text or text in {"无明显周期影响", "不处于生理期", "生理期模拟未开启"}:
        return neutral
    phase = next(
        (
            str(item.get("phase") or "")
            for item in state.get("conditions", ())
            if isinstance(item, dict) and item.get("kind") == "body_cycle"
        ),
        "",
    )
    if not phase:
        for word, candidate in (
            ("PMS", "pms"),
            ("经前综合征", "pms"),
            ("排卵前期", "pre_ovulation"),
            ("月经期", "menstrual"),
            ("卵泡期", "follicular"),
            ("排卵期", "ovulation"),
            ("黄体期", "luteal"),
            ("后", "recovery"),
            ("恢复", "recovery"),
            ("前", "pre"),
            ("生理期", "period"),
        ):
            if word in text.upper():
                phase = candidate
                break
    return (
        (1.08, 0.92)
        if phase == "recovery"
        else (1.12, 0.88)
        if phase in {"pre", "pms"}
        else (1.25, 0.76)
        if phase in {"period", "menstrual"}
        else neutral
    )


def atmosphere(recent, *, at, reset_at=0):
    """Calculate the V1 twelve-minute mood and pace without a model call.

    Args:
        recent: Filtered V1-shaped member records.
        at: Epoch timestamp of the current message.
        reset_at: Explicit atmosphere reset timestamp.

    Returns:
        Mood and pace used by the original interjection gate.
    """
    start = max(at - 720, reset_at if reset_at <= at else 0)
    window = [item for item in recent if start < item["ts"] <= at]
    speakers = len({str(item.get("sender_id") or "") for item in window})
    text = "\n".join(str(item.get("text") or "") for item in window)
    pace = (
        "热闹"
        if len(window) >= 18 or speakers >= 6
        else "有来有回"
        if len(window) >= 6
        else "安静"
    )
    mood = "玩笑" if re.search(r"(哈哈|笑死|草|乐|绷|hhh)", text, re.I) else "平稳"
    if (
        re.search(r"(别吵|吵架|争吵|闭嘴|骂人|生气|急眼|烦死)", text)
        or speakers >= 2
        and len(re.findall(r"(烦|累|难受|着急)", text)) >= 2
    ):
        mood = "紧绷"
    if re.search(r"(求助|怎么|为什么|报错|帮|救命)", text):
        mood = "求助"
    return {"mood": mood, "pace": pace}


def interjection_probability(
    group,
    text,
    *,
    at,
    today,
    max_daily,
    interval_minutes,
    daily_state,
    cycle_enabled,
    talking_to="group",
):
    """Return zero for a closed V1 gate and its exact probability otherwise.

    Args:
        group: Persisted V1 daily count, last send and atmosphere.
        text: Current authenticated group text.
        at: Current epoch timestamp.
        today: Local calendar day.
        max_daily: Effective daily quota, with 999999 meaning unlimited.
        interval_minutes: Effective base interval before cycle modulation.
        daily_state: Current compatible role state inputs.
        cycle_enabled: Effective cycle simulation switch.
        talking_to: Structured current message recipient.

    Returns:
        Probability in the inclusive range zero to one.
    """
    if max_daily <= 0 or (
        max_daily < 999999
        and group.get("interject_day") == today
        and group.get("interject_today", 0) >= max_daily
    ):
        return 0.0
    interval_factor, probability_factor = cycle_profile(
        daily_state, enabled=cycle_enabled, today=today
    )
    interval = max(1, min(1440, round(interval_minutes * interval_factor)))
    if at - group.get("last_interject_at", 0) < interval * 60:
        return 0.0
    if talking_to not in {"", "group", "bot"} or re.search(
        r"^\s*(?:@|回复|引用)", text
    ):
        return 0.0
    if re.search(r"(别插|别接|别吵|别回|闭嘴|别打断)", text):
        return 0.0
    if re.search(r"https?://|www\.|\[转发消息\]|\[分享", text, re.I):
        return 0.0
    mood = group.get("atmosphere", {}).get("mood", "")
    if group.get("atmosphere", {}).get("pace") == "热闹" and mood not in {
        "玩笑",
        "求助",
    }:
        return 0.0
    probability = (
        0.055
        if re.search(r"(有没有人|谁懂|救命|怎么回事|咋办)", text)
        else (0.018 if mood == "玩笑" else 0.008)
        if re.search(r"(笑死|绷不住|太离谱)", text)
        else 0.015
        if mood == "玩笑"
        else 0.035
        if mood == "求助"
        else 0.0
    )
    return max(0.0, min(1.0, probability * probability_factor))


class LegacyGroupRuntime:
    """Persist migrated admission inputs and receipt-based native counters."""

    def __init__(self, runtime):
        self.runtime = runtime
        self.store = GroupPresenceStore(runtime)

    @staticmethod
    def role_key(context):
        """Return the shared role key without crossing bots or installations.

        Args:
            context: Authorized role/group context.

        Returns:
            Runtime metadata key for this role's migrated admission inputs.
        """
        return ROLE_PREFIX + digest(
            [
                context.scope.installation_id,
                context.scope.bot_id,
                context.role_binding.role.subject_id,
            ]
        )

    def read(self, context):
        """Read imported inputs, allowing new groups to inherit only role inputs.

        Args:
            context: Authorized group context.

        Returns:
            Group record and shared role inputs, or None before reviewed import.
        """
        row = self.runtime.read_one(
            "SELECT value FROM runtime_metadata WHERE key = ?",
            (self.role_key(context),),
        )
        if row is None:
            return None
        role = json.loads(row["value"])
        group = self.store.read(owner_key(context), KIND) or {
            "last_seen": 0,
            "previous_seen": 0,
            "observed_event": "",
            "interject_day": "",
            "interject_today": 0,
            "native_repeat_today": 0,
            "last_interject_at": 0,
            "recent": [],
            "atmosphere": {},
            "trials": {},
        }
        return group, role

    def observe(self, context, *, text, speaker_id, received_at, event_id):
        """Advance last-seen and atmosphere once per authenticated source event.

        Args:
            context: Authorized group context.
            text: Current member text.
            speaker_id: Native speaker identity.
            received_at: Original aware source timestamp.
            event_id: Stable source event identifier.
        """
        with self.runtime.transaction() as connection:
            saved = self.read(context)
            if saved is None:
                return
            group, role = saved
            when = received_at.timestamp()
            event = event_id or digest([stamp(received_at), speaker_id, text])
            if event == group.get("observed_event") or when < group.get("last_seen", 0):
                return
            if any(item.get("event_id") == event for item in group.get("recent", ())):
                return
            group.update(
                previous_seen=group.get("last_seen", 0),
                last_seen=when,
                observed_event=event,
            )
            group["recent"] = [
                *group.get("recent", ()),
                {"ts": when, "sender_id": speaker_id, "text": text, "event_id": event},
            ][-role["history_limit"] :]
            group["atmosphere"] = atmosphere(
                group["recent"], at=when, reset_at=group.get("atmosphere_reset_at", 0)
            )
            self.store.write(connection, owner_key(context), KIND, group)

    def cold_ready(self, context, *, at, event_id, idle_minutes):
        """Apply the old last-seen, four sleep phases and energy boundary.

        Args:
            context: Authorized group context.
            at: Original source time.
            event_id: Stable source event identifier.
            idle_minutes: Effective cold-group idle threshold.

        Returns:
            Whether the deterministic state and idle gates are open.
        """
        saved = self.read(context)
        if saved is None:
            return False
        group, role = saved
        last = (
            group.get("previous_seen", 0)
            if event_id == group.get("observed_event")
            else group.get("last_seen", 0)
        )
        state = role["daily_state"]
        return (
            last > 0
            and at.timestamp() - last >= max(3, idle_minutes) * 60
            and state.get("sleep_runtime", {}).get("phase", "") not in SLEEP_PHASES
            and state.get("energy", 70) > 35
        )

    def interject(self, context, text, *, at, event_id, values, timezone_name):
        """Sample a V1 candidate once; retries cannot reroll the same event.

        Args:
            context: Authorized group context.
            text: Current member text.
            at: Original source timestamp.
            event_id: Stable source event identifier.
            values: Effective registered group participation settings.
            timezone_name: Installation local calendar timezone.

        Returns:
            Whether the original hard gates and random trial admit this event.
        """
        with self.runtime.transaction() as connection:
            saved = self.read(context)
            if saved is None:
                return False
            group, role = saved
            event = event_id or digest([context.key, stamp(at), text])
            trials = group.setdefault("trials", {})
            if event in trials:
                return trials[event]
            probability = interjection_probability(
                group,
                text,
                at=at.timestamp(),
                today=day_key(at, timezone_name),
                max_daily=values["group_interject_max_daily"],
                interval_minutes=values["group_interject_min_interval_minutes"],
                daily_state=role["daily_state"],
                cycle_enabled=role["cycle_enabled"],
            )
            admitted = probability > 0 and random.random() < probability
            trials[event] = admitted
            group["trials"] = dict(tuple(trials.items())[-128:])
            self.store.write(connection, owner_key(context), KIND, group)
            return admitted

    def record_returned(self, connection, context, turn, *, at, timezone_name):
        """Count the first successful native send inside its receipt transaction.

        Args:
            connection: Caller-owned runtime transaction.
            context: Authorized group context.
            turn: Native turn before its first send return is recorded.
            at: Observed send-return timestamp.
            timezone_name: Installation local calendar timezone.
        """
        if (
            turn.get("send_returns", 0)
            or turn.get("mentioned")
            or turn.get("reason") not in {"interjection", "repeat"}
        ):
            return
        saved = self.read(context)
        if saved is None:
            return
        group, _ = saved
        today = day_key(at, timezone_name)
        if group.get("interject_day") != today:
            group.update(interject_day=today, interject_today=0, native_repeat_today=0)
        group["interject_today"] += 1
        group["native_repeat_today"] = group.get("native_repeat_today", 0) + (
            turn.get("reason") == "repeat"
        )
        group["last_interject_at"] = at.timestamp()
        self.store.write(connection, owner_key(context), KIND, group)


def build_import(root, *, source_persona_id, source_group_id, configuration=None):
    """Read only admission inputs from the explicitly selected source profile.

    Args:
        root: Read-only V1 data root.
        source_persona_id: Reviewed source persona, empty for the primary profile.
        source_group_id: Exact native source group identity.
        configuration: Optional original configuration for source selection.

    Returns:
        Fingerprinted group and role admission inputs, with lifecycle limitation.

    Raises:
        TaskError: The selected profile or group is unavailable.
    """
    from companion_v2.legacy.data_migration import persona_profile_settings
    from companion_v2.legacy.configuration_migration import _source_value

    payload, reason, _ = persona_profile_settings(
        root, configuration, source_persona_id
    )
    group = (
        payload.get("groups", {}).get(source_group_id)
        if isinstance(payload, dict)
        else None
    )
    if not isinstance(group, dict):
        raise TaskError(
            "legacy_group_runtime_unavailable", reason or "legacy group is unavailable"
        )
    source = payload.get("daily_state") or {}
    state = {
        "date": str(source.get("date") or ""),
        "energy": max(0, min(100, int(source.get("energy", 70)))),
        "sleep_runtime": {
            "phase": str((source.get("sleep_runtime") or {}).get("phase") or "")
        },
        "body_cycle": str(source.get("body_cycle") or ""),
        "conditions": [
            {"kind": "body_cycle", "phase": str(item.get("phase") or "")}
            for item in source.get("conditions", ())
            if isinstance(item, dict) and item.get("kind") == "body_cycle"
        ],
    }
    config = configuration or {}
    _, history_limit = _source_value(
        config, "relay_bridge_config.max_group_recent_messages"
    )
    _, cycle_enabled = _source_value(
        config, "humanized_state_config.enable_cycle_state"
    )
    role = {
        "daily_state": state,
        "history_limit": max(1, int(history_limit or 80)),
        "cycle_enabled": True if cycle_enabled is None else bool(cycle_enabled),
    }
    recent = [
        {
            "ts": float(item.get("ts") or 0),
            "sender_id": str(item.get("sender_id") or ""),
            "text": str(item.get("text") or ""),
        }
        for item in group.get("recent_messages", ())
        if isinstance(item, dict) and not item.get("injection_guard_blocked")
    ]
    record = {
        "last_seen": float(group.get("last_seen") or 0),
        "previous_seen": 0,
        "observed_event": "",
        "interject_day": str(group.get("interject_day") or ""),
        "interject_today": max(0, int(group.get("interject_today") or 0)),
        "native_repeat_today": 0,
        "last_interject_at": float(group.get("last_interject_at") or 0),
        "recent": recent[-role["history_limit"] :],
        "atmosphere": deepcopy(group.get("atmosphere") or {}),
        "atmosphere_reset_at": float(
            (group.get("atmosphere") or {}).get("reset_at") or 0
        ),
        "trials": {},
    }
    return {
        "fingerprint": digest([record, role]),
        "policy_fingerprint": digest(
            [
                {
                    key: record[key]
                    for key in (
                        "last_seen",
                        "interject_day",
                        "interject_today",
                        "last_interject_at",
                    )
                },
                role,
            ]
        ),
        "group": record,
        "role": role,
        "state_evolution_equivalent": False,
    }


def import_reviewed(runtime, plan, context):
    """Seed reviewed inputs once without resetting subsequent live counters.

    Args:
        runtime: Destination runtime store.
        plan: Exact fingerprinted source admission plan.
        context: Explicit destination group and role mapping.

    Returns:
        Idempotent seed receipt and the unresolved state lifecycle limitation.

    Raises:
        TaskError: A different source attempts to overwrite live admission state.
    """
    service = LegacyGroupRuntime(runtime)
    with runtime.transaction() as connection:
        previous = service.store.read(owner_key(context), KIND)
        if (
            previous
            and previous.get("source_policy_fingerprint") != plan["policy_fingerprint"]
        ):
            raise TaskError(
                "legacy_group_runtime_conflict",
                "live group admission state cannot be overwritten",
            )
        if not previous:
            service.store.write(
                connection,
                owner_key(context),
                KIND,
                {
                    **plan["group"],
                    "source_fingerprint": plan["fingerprint"],
                    "source_policy_fingerprint": plan["policy_fingerprint"],
                },
            )
        role_key = service.role_key(context)
        old_role = runtime.read_one(
            "SELECT value FROM runtime_metadata WHERE key = ?", (role_key,)
        )
        if old_role is not None and json.loads(old_role["value"]) != plan["role"]:
            raise TaskError(
                "legacy_group_runtime_conflict", "shared role admission inputs changed"
            )
        connection.execute(
            "INSERT OR IGNORE INTO runtime_metadata(key, value) VALUES (?, ?)",
            (role_key, json.dumps(plan["role"], ensure_ascii=False, sort_keys=True)),
        )
    return {
        "state": (
            "preserved_runtime"
            if previous and previous.get("source_fingerprint") != plan["fingerprint"]
            else "unchanged"
            if previous
            else "imported"
        ),
        "fingerprint": plan["fingerprint"],
        "runtime_source_fingerprint": previous["source_fingerprint"]
        if previous
        else plan["fingerprint"],
        "state_evolution_equivalent": False,
    }

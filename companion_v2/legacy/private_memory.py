"""Read REQ-041 private memory without reviving its jobs or current state."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

from .profile_source import primary_profile
from .sqlite_snapshot import open_readonly_sqlite_snapshot


SOURCE_ID = "private.authoritative_memory"
SCHEMA = "req041.person_private_memory.v1"
IDENTITY_FIELDS = (
    "companion_instance_id",
    "bot_account_id",
    "adapter_instance_id",
    "subject_namespace",
    "platform_subject_id",
)
HISTORY_FIELDS = frozenset(
    (
        "companion_memory",
        "dialogue_episodes",
        "open_loops",
        "behavior_habits",
        "action_preferences",
        "action_consequences",
        "state_continuity",
        "recent_reply_topics",
        "intent_profile",
        "birthday_profile",
        "memory_corrections",
    )
)


def canonical(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def moment(value):
    try:
        if type(value) in (int, float):
            return datetime.fromtimestamp(value, timezone.utc).isoformat()
        if isinstance(value, str) and value.strip():
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return (
                (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc))
                .astimezone(timezone.utc)
                .isoformat()
            )
    except (ValueError, TypeError, OverflowError, OSError):
        pass
    return ""


def _materials(person_id, record):
    result = []
    for field, value in record["content"].items():
        if (
            field not in HISTORY_FIELDS and not field.startswith("relationship_")
        ) or value in (None, "", {}, []):
            continue
        if field == "companion_memory" and isinstance(value, dict):
            entries = [("items", item) for item in value.get("items", ())]
            entries.extend(
                (key, item)
                for key, item in value.items()
                if key not in ("items", "updated_at", "profile_updated_at")
            )
        elif field == "behavior_habits" and isinstance(value, dict):
            entries = [("patterns", item) for item in value.get("patterns", ())]
        else:
            entries = (
                [(field, item) for item in value]
                if isinstance(value, list)
                else [(field, value)]
            )
        for index, (part, item) in enumerate(entries):
            if item in (None, "", {}, []):
                continue
            attributes = item if isinstance(item, dict) else {}
            occurred = next(
                (
                    date
                    for key in (
                        "created_ts",
                        "created_at",
                        "date",
                        "ts",
                        "updated_at",
                        "last_seen_ts",
                    )
                    if (date := moment(attributes.get(key)))
                ),
                moment(record.get("updated_at")),
            )
            text = item if isinstance(item, str) else canonical(item)
            # Preserve every character; oversized historical documents are paged, not truncated.
            for offset in range(0, len(text), 24000):
                result.append(
                    {
                        "key": [person_id, field, part, index, offset],
                        "kind": "timeline"
                        if field
                        in (
                            "dialogue_episodes",
                            "action_consequences",
                            "recent_reply_topics",
                        )
                        else "memories",
                        "occurred_at": occurred,
                        "text": text[offset : offset + 24000],
                        "attributes": {
                            "source_id": SOURCE_ID,
                            "source_person_id": person_id,
                            "source_field": field,
                            "source_revision": record["revision"],
                            "source_part": part,
                            "source_index": index,
                            "source_offset": offset,
                            "source_updated_at": record.get("updated_at", ""),
                            "source_container_metadata": {
                                key: value
                                for key, value in value.items()
                                if key
                                != (
                                    "items"
                                    if field == "companion_memory"
                                    else "patterns"
                                )
                            }
                            if isinstance(value, dict)
                            and field in ("companion_memory", "behavior_habits")
                            else {},
                            "historical_only": True,
                            "execution_restored": False,
                        },
                    }
                )
    return result


@dataclass(frozen=True)
class PrivateMemoryPlan:
    source_fingerprint: str
    lineage_id: str
    records: tuple[dict, ...]
    rejected: tuple[dict, ...]
    source_error: str = ""
    continuity_blockers: tuple[dict, ...] = ()
    authority: str = "req041_private_memory"

    def document(self):
        return {
            "source_id": SOURCE_ID,
            "state": "unavailable" if self.source_error else "ready",
            "reason": self.source_error,
            "fingerprint": self.source_fingerprint,
            "record_count": len(self.records),
            "material_count": sum(len(r["materials"]) for r in self.records),
            "records": [
                {k: r[k] for k in ("person_id", "revision", "identities")}
                | {"material_count": len(r["materials"])}
                for r in self.records
            ],
            "rejected": list(self.rejected),
            "historical_only": True,
            "execution_restored": False,
            "authority": self.authority,
            "continuity_blockers": list(self.continuity_blockers),
            "cutover_ready": not self.source_error
            and not self.rejected
            and not self.continuity_blockers,
        }

    def matching(
        self, *, account_id, native_actor_id, platform=None, native_identity=None
    ):
        native_identity = native_identity if isinstance(native_identity, dict) else {}
        return tuple(
            r
            for r in self.records
            if any(
                i["platform_subject_id"] == native_actor_id
                and i["subject_namespace"]
                in (
                    "aiocqhttp:user",
                    "onebot:user",
                    "qq_official:user",
                    "qq_official_webhook:user",
                )
                and (platform is None or i["adapter_instance_id"] == platform)
                and (
                    (
                        i["bot_account_id"].split(":", 1)[-1] == account_id
                        and i["subject_namespace"] in ("aiocqhttp:user", "onebot:user")
                    )
                    or (
                        i["subject_namespace"]
                        in ("qq_official:user", "qq_official_webhook:user")
                        and platform is not None
                        and native_identity.get("bot_id")
                        == i["bot_account_id"].split(":", 1)[-1]
                        and native_identity.get("platform") == platform
                        and native_identity.get("account_id") == account_id
                        and native_identity.get("user_id") == native_actor_id
                        and native_identity.get("group_id") == ""
                    )
                )
                for i in r["identities"]
            )
        )


def _legacy_user_plan(payload, lineage, control, configuration):
    """Read authoritative private user history without replaying legacy work.

    Args:
        payload: The selected legacy profile document.
        lineage: Stable source identity used for replacement imports.
        control: Verified REQ-041 read-generation rows, or an empty list.
        configuration: The unmodified legacy configuration supplied for review.

    Returns:
        A plan with exact private identities and explicit continuity gaps.
    """
    from .private_contacts import private_umo, verified_private_identity
    from companion_v2.execution.models import digest

    records, rejected, blockers, identities_seen = [], [], [], {}
    configuration = configuration if isinstance(configuration, dict) else {}
    policy = {}
    for values in (
        configuration,
        *(value for value in configuration.values() if isinstance(value, dict)),
    ):
        policy.update(
            {
                key: value
                for key, value in values.items()
                if key.startswith("relationship_")
                or key.startswith("enable_relationship_")
                or key
                in (
                    "enable_custom_relationship_stage_policy",
                    "enable_p4_b_legacy_score_isolation",
                    "environment_perception_timezone",
                    "enable_open_loop_tracking",
                )
            }
        )
    users = payload.get("users", {})
    if not isinstance(users, dict):
        return PrivateMemoryPlan("", lineage, (), (), "private_memory_users_invalid")
    for key, user in users.items():
        if not isinstance(user, dict):
            rejected.append(
                {"source_key": str(key), "reason": "private_memory_user_invalid"}
            )
            continue
        content = {
            field: value
            for field, value in user.items()
            if field in HISTORY_FIELDS or field.startswith("relationship_")
        }
        if not any(value not in (None, "", {}, []) for value in content.values()):
            continue
        verified = verified_private_identity(user)
        if (
            verified is None
            or not private_umo(user)
            or user.get("observation_only")
            or user.get("profile_origin") == "group_observation"
        ):
            rejected.append(
                {"source_key": str(key), "reason": "private_identity_unverified"}
            )
            continue
        platform, account, participant = verified
        namespace = user["identity_platform_kind"]
        identity = {
            "companion_instance_id": "astrbot_plugin_private_companion",
            "bot_account_id": namespace + ":" + account,
            "adapter_instance_id": platform,
            "subject_namespace": namespace + ":user",
            "platform_subject_id": participant,
        }
        person_id = "legacy-user:" + fingerprint(identity)
        if person_id in identities_seen:
            if identities_seen[person_id] != content:
                rejected.append(
                    {"source_key": str(key), "reason": "private_memory_alias_conflict"}
                )
                records = [
                    record for record in records if record["person_id"] != person_id
                ]
            continue
        identities_seen[person_id] = content
        record = {
            "revision": 1,
            "content": content,
            "updated_at": user.get("last_memory_refresh_at", ""),
        }
        gaps = []
        if "relationship_score" in content or content.get("relationship_ledger"):
            gaps.append("relationship_progression_not_equivalent")
            if policy.get("environment_perception_timezone") == "global":
                gaps.append("relationship_timezone_unresolved")
        loops = content.get("open_loops")
        if loops and (
            not isinstance(loops, list)
            or any(
                not isinstance(item, dict)
                or item.get("status")
                not in (
                    "closed",
                    "completed",
                    "done",
                    "cancelled",
                    "resolved",
                    "已完成",
                    "已取消",
                )
                for item in loops
            )
        ):
            gaps.append("open_loop_continuity_not_equivalent")
        blockers.extend({"person_id": person_id, "reason": reason} for reason in gaps)
        records.append(
            {
                "person_id": person_id,
                "revision": 1,
                "identities": [identity],
                "materials": _materials(person_id, record),
                "legacy_state": content,
                "relationship_policy": policy,
                "source_config_digest": digest(configuration),
                "relationship_interaction": {
                    field: user[field]
                    for field in (
                        "awaiting_reply_since",
                        "reply_count",
                        "last_reply_at",
                        "last_private_reply_at",
                        "last_proactive_action",
                        "episode_message_count",
                    )
                    if field in user
                },
                "last_activity_at": user.get("last_activity_at"),
                "last_seen": user.get("last_seen"),
            }
        )
    return PrivateMemoryPlan(
        fingerprint([users, control, policy]),
        lineage,
        tuple(records),
        tuple(rejected),
        continuity_blockers=tuple(blockers),
        authority="users",
    )


def build_private_memory_plan(root: Path, configuration=None):
    source = primary_profile(Path(root), configuration)
    lineage = fingerprint(
        [
            SOURCE_ID,
            str(source.path.resolve()) if source.path else str(Path(root).resolve()),
        ]
    )
    if source.payload is None:
        return PrivateMemoryPlan("", lineage, (), (), source.error)
    payload = source.payload
    users = payload.get("users", {})
    control_path = Path(root) / "req041_migration_control.db"
    control = []
    if users:
        if control_path.exists():
            try:
                connection = open_readonly_sqlite_snapshot(control_path)
                try:
                    if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                        raise sqlite3.DatabaseError("invalid control database")
                    control = connection.execute(
                        "SELECT identity_id,state,read_generation FROM migration_identities ORDER BY identity_id"
                    ).fetchall()
                finally:
                    connection.close()
            except (OSError, sqlite3.Error):
                return PrivateMemoryPlan(
                    "", lineage, (), (), "private_memory_authority_unverified"
                )
            if any(row[2] != "legacy" for row in control):
                return PrivateMemoryPlan(
                    "", lineage, (), (), "private_memory_mixed_generation_unsupported"
                )
        elif isinstance(payload.get("_req041_private_memory"), dict) and payload[
            "_req041_private_memory"
        ].get("records"):
            return PrivateMemoryPlan(
                "", lineage, (), (), "private_memory_authority_unverified"
            )
        return _legacy_user_plan(payload, lineage, control, configuration)
    store = payload.get("_req041_private_memory", {"schema": SCHEMA, "records": {}})
    registry = payload.get("unified_person", {})
    if (
        not isinstance(store, dict)
        or store.get("schema") != SCHEMA
        or not isinstance(store.get("records"), dict)
        or not isinstance(registry, dict)
    ):
        return PrivateMemoryPlan("", lineage, (), (), "private_memory_store_invalid")
    records, rejected = [], []
    profiles, links = registry.get("profiles", {}), registry.get("identity_links", {})
    for person_id, record in store["records"].items():
        reason, identities = "", []
        profile = profiles.get(person_id, {}) if isinstance(profiles, dict) else {}
        try:
            if (
                not isinstance(record, dict)
                or record.get("schema") != SCHEMA
                or type(record.get("revision")) is not int
                or record["revision"] < 1
                or not isinstance(record.get("content"), dict)
                or record.get("content_hash") != fingerprint(record["content"])
            ):
                raise ValueError("private_memory_record_invalid")
            if (
                not isinstance(profile, dict)
                or profile.get("profile_status") != "active"
                or person_id in registry.get("person_tombstones", {})
            ):
                raise ValueError("private_memory_person_inactive")
            for key in profile.get("identity_keys", ()):
                link = links.get(key, {}) if isinstance(links, dict) else {}
                identity = link.get("identity", {}) if isinstance(link, dict) else {}
                if (
                    not isinstance(identity, dict)
                    or set(identity) != set(IDENTITY_FIELDS)
                    or any(
                        not isinstance(identity[k], str) or not identity[k]
                        for k in IDENTITY_FIELDS
                    )
                    or link.get("status") != "active"
                    or link.get("person_id") != person_id
                    or key in registry.get("identity_tombstones", {})
                ):
                    continue
                if key == "chat-origin-v1:" + fingerprint(identity):
                    identities.append(identity)
            if not identities:
                raise ValueError("private_memory_identity_unresolved")
            records.append(
                {
                    "person_id": person_id,
                    "revision": record["revision"],
                    "identities": identities,
                    "materials": _materials(person_id, record),
                }
            )
        except (ValueError, TypeError, OverflowError):
            reason = "private_memory_record_or_identity_invalid"
        if reason:
            rejected.append({"person_id": person_id, "reason": reason})
    return PrivateMemoryPlan(
        fingerprint(
            [
                store,
                registry.get("profiles", {}),
                links,
                registry.get("person_tombstones", {}),
                registry.get("identity_tombstones", {}),
            ]
        ),
        lineage,
        tuple(records),
        tuple(rejected),
    )

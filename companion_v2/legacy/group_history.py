"""Read complete V1 group history for explicitly reviewed group mappings."""

from dataclasses import dataclass
from pathlib import Path

from .data_migration import persona_profile_settings
from .private_memory import canonical, fingerprint, moment


SOURCE_ID = "group.authoritative_history"
HISTORY_FIELDS = ("recent_messages", "group_episodes", "episodes", "topic_threads")


@dataclass(frozen=True)
class GroupHistoryPlan:
    source_fingerprint: str
    lineage_id: str
    records: tuple[dict, ...]
    counts: dict
    rejected: tuple[dict, ...] = ()
    source_error: str = ""
    continuity_blockers: tuple[dict, ...] = ()

    def document(self):
        """Return review metadata without exposing group message contents.

        Returns:
            Counts, source fingerprint and explicit history-only semantics.
        """
        return {
            "source_id": SOURCE_ID,
            "fingerprint": self.source_fingerprint,
            "state": "unavailable" if self.source_error else "ready",
            "reason": self.source_error,
            "counts": self.counts,
            "material_count": sum(len(row["materials"]) for row in self.records),
            "rejected": list(self.rejected),
            "historical_only": True,
            "execution_restored": False,
            "consumer": "group.memory.legacy.query",
        }


def build_group_history_plan(
    root, *, source_persona_id, source_group_id, configuration=None
):
    """Preserve each group record and its timestamps without restarting old work.

    Args:
        root: Read-only V1 data root.
        source_persona_id: Explicit source persona, empty for the primary profile.
        source_group_id: Exact native source group ID, including opaque QQ IDs.
        configuration: Optional original profile authority settings.

    Returns:
        Complete history records with original field/index and bounded chunks.
    """
    payload, reason, relative = persona_profile_settings(
        root, configuration, source_persona_id
    )
    lineage = fingerprint(
        [
            SOURCE_ID,
            str(Path(root).resolve()),
            relative,
            source_persona_id,
            source_group_id,
        ]
    )
    if payload is None:
        return GroupHistoryPlan("", lineage, (), {}, source_error=reason)
    groups = payload.get("groups", {})
    group = groups.get(source_group_id) if isinstance(groups, dict) else None
    if not isinstance(group, dict):
        return GroupHistoryPlan(
            "", lineage, (), {}, source_error="legacy_group_not_found"
        )
    selected = {field: group[field] for field in HISTORY_FIELDS if field in group}
    materials, rejected, counts = [], [], {}
    identifier = "legacy-group:" + fingerprint([source_persona_id, source_group_id])
    for field, values in selected.items():
        if not isinstance(values, (list, dict)):
            rejected.append(
                {"field": field, "reason": "group_history_collection_invalid"}
            )
            continue
        entries = (
            list(values.items())
            if isinstance(values, dict)
            else list(enumerate(values))
        )
        counts[field] = len(entries)
        for index, item in entries:
            raw = item if isinstance(item, dict) else {}
            occurred = next(
                (
                    date
                    for key in (
                        "ts",
                        "created_ts",
                        "created_at",
                        "date",
                        "last_ts",
                        "started_ts",
                        "updated_at",
                    )
                    if (date := moment(raw.get(key)))
                ),
                "",
            )
            # Canonical JSON keeps scalar types, speaker IDs and all status fields.
            text = canonical(item)
            for offset in range(0, len(text), 24000):
                materials.append(
                    {
                        "key": [identifier, field, index, offset],
                        "kind": "memories" if field == "topic_threads" else "timeline",
                        "occurred_at": occurred,
                        "text": text[offset : offset + 24000],
                        "attributes": {
                            "source_id": SOURCE_ID,
                            "source_persona_id": source_persona_id,
                            "source_group_id": source_group_id,
                            "source_field": field,
                            "source_index": index,
                            "source_offset": offset,
                            "historical_only": True,
                            "execution_restored": False,
                        },
                    }
                )
    # A valid empty source is still a selected group generation. Replacing it
    # must remove older imported material from the same reviewed lineage.
    records = ({"person_id": identifier, "materials": materials},)
    return GroupHistoryPlan(
        fingerprint(selected), lineage, records, counts, tuple(rejected)
    )


def import_group_history(store, plan, target, *, preview=False):
    """Import reviewed group material into its exact group historical reader.

    Args:
        store: Historical material store for the destination.
        plan: Reviewed group history plan.
        target: Exact group/role/platform/account destination mapping.
        preview: Return counts without writing when true.

    Returns:
        Historical import receipt, including idempotence and full row counts.

    Raises:
        ValueError: The source is invalid or the destination is not a group.
    """
    from companion_v2.memory.private_legacy_import import import_reviewed_materials

    if target.get("scope_kind") != "group":
        raise ValueError("legacy_group_history_group_target_required")
    if plan.source_error or plan.rejected:
        raise ValueError("legacy_group_history_source_invalid")
    return import_reviewed_materials(
        store, plan, plan.records, target, source_id=SOURCE_ID, preview=preview
    )

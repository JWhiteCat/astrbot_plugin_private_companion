"""Install reviewed V1 person-private records in the historical read model."""

from datetime import datetime, timedelta, timezone
import json

from companion_v2.execution.models import TaskError, digest, encode, require
from .legacy_materials import _database, _target_document, _utc


def import_private_memory(store, plan, records, target, *, preview=False):
    target = _target_document(target)
    if target["scope_kind"] != "private":
        raise ValueError("legacy_private_memory_private_target_required")
    return import_reviewed_materials(
        store,
        plan,
        records,
        target,
        source_id="private.authoritative_memory",
        preview=preview,
    )


def import_reviewed_materials(
    store, plan, records, target, *, source_id, preview=False
):
    """Install exact reviewed records in a scope-bound historical projection.

    Args:
        store: Historical material store for this installation.
        plan: Validated source with stable lineage and complete material rows.
        records: Exact selected records belonging to the plan.
        target: Explicit installation, role, account and private/group scope.
        source_id: Source contract identifier for the import receipt.
        preview: Return the receipt without writing when true.

    Returns:
        Import counts and unresolved continuity requirements.

    Raises:
        ValueError: The target or selection is invalid.
    """
    target = _target_document(target)
    selection = sorted(r["person_id"] for r in records)
    if not selection or not set(selection).issubset(
        {r["person_id"] for r in plan.records}
    ):
        raise ValueError("legacy_private_memory_selection_invalid")
    if any(record not in plan.records for record in records):
        raise ValueError("legacy_private_memory_selection_invalid")
    # Each destination has its own lineage; importing a second role never erases the first.
    lineage = digest([plan.lineage_id, target])
    generation = digest([plan.source_fingerprint, target, selection])
    binding = digest([selection, target])
    rows = []
    for record in records:
        for item in record["materials"]:
            rows.append(
                (
                    generation,
                    digest([generation, item["key"]]),
                    item["kind"],
                    target["installation_id"],
                    target["bot_id"],
                    target["role_id"],
                    target["actor_id"],
                    target["scope_kind"],
                    target["native_scope_id"],
                    target["platform"],
                    target["account_id"],
                    _utc(item["occurred_at"]),
                    item["text"],
                    encode(item["attributes"]),
                )
            )
    result = {
        "source_id": source_id,
        "source_fingerprint": generation,
        "selected_person_count": len(selection),
        "imported": {
            "memories": sum(r[2] == "memories" for r in rows),
            "timeline": sum(r[2] == "timeline" for r in rows),
        },
        "historical_only": True,
        "execution_restored": False,
        "already_present": False,
    }
    result["continuity_blockers"] = [
        item for item in plan.continuity_blockers if item["person_id"] in selection
    ]
    result["cutover_ready"] = not result["continuity_blockers"] and not plan.rejected
    if preview:
        return result
    connection = _database(store.runtime)
    try:
        connection.execute("BEGIN IMMEDIATE")
        prior = connection.execute(
            "SELECT stats_json FROM legacy_memory_imports WHERE source_fingerprint=?",
            (generation,),
        ).fetchone()
        if prior:
            connection.execute("ROLLBACK")
            return {**json.loads(prior["stats_json"]), "already_present": True}
        old = [
            r[0]
            for r in connection.execute(
                "SELECT source_fingerprint FROM legacy_memory_imports WHERE lineage_id=?",
                (lineage,),
            )
        ]
        for value in old:
            connection.execute(
                "DELETE FROM legacy_memory_materials WHERE source_fingerprint=?",
                (value,),
            )
            connection.execute(
                "DELETE FROM legacy_memory_imports WHERE source_fingerprint=?", (value,)
            )
        connection.execute(
            "INSERT INTO legacy_memory_imports VALUES (?,?,?,?,?,?)",
            (
                generation,
                plan.source_fingerprint,
                binding,
                datetime.now(timezone.utc).isoformat(),
                encode(result),
                lineage,
            ),
        )
        connection.executemany(
            "INSERT INTO legacy_memory_materials VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        connection.execute("COMMIT")
        return result
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


def import_private_continuity(
    store, plan, records, context, *, observed_at, operator=None, preview=False
):
    """Restore legacy open items and keep the complete relationship account.

    Args:
        store: The authorized historical-memory store for the destination.
        plan: The validated source plan.
        records: Exact source records selected for this private identity.
        context: The current authenticated private context.
        observed_at: The migration time used for authorization.
        operator: Authenticated control-plane actor authorizing restored memo access.
        preview: Validate authority and destination conflicts without changing state.

    Returns:
        A receipt describing restored objects and remaining equivalence gaps.

    Raises:
        TaskError: Identity, source state, or an existing destination conflicts.
    """
    from companion_v2.agenda.models import AgendaTarget, Memo
    from companion_v2.agenda.service import AgendaService
    from companion_v2.legacy.private_memory import moment
    from companion_v2.legacy.private_contacts import native_private_identity
    from companion_v2.memory.models import owner_key

    store.writer.access.check(context, at=observed_at)
    if any(record not in plan.records for record in records):
        raise TaskError(
            "legacy_private_memory_selection_invalid", "source records changed"
        )
    selected = [record for record in records if "legacy_state" in record]
    if not selected:
        return {
            "restored_open_loops": 0,
            "continuity_blockers": [],
            "already_present": False,
        }
    if len(selected) != 1:
        raise TaskError(
            "legacy_private_continuity_ambiguous",
            "select one exact legacy private account",
        )
    record = selected[0]
    if record not in plan.matching(
        account_id=context.scope.account_id,
        native_actor_id=context.actor.principal.native_account_id,
        platform=context.scope.platform,
        native_identity=native_private_identity(
            store.runtime,
            context.scope.platform,
            context.actor.principal.native_account_id,
        ),
    ):
        raise TaskError(
            "legacy_private_memory_identity_unmatched",
            "source belongs to another private identity",
        )
    key = "legacy_private_continuity:" + owner_key(context)
    state = record["legacy_state"]
    loops = state.get("open_loops") or []
    if not isinstance(loops, list):
        raise TaskError("legacy_open_loop_invalid", "legacy open items must be a list")
    agenda = AgendaService(store.runtime)
    target = AgendaTarget(
        context.scope, context.role_binding.role, context.actor.subject
    )
    memos, origins = [], []
    for index, item in enumerate(loops):
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("text"), str)
            or not item["text"].strip()
        ):
            raise TaskError(
                "legacy_open_loop_invalid", "legacy open item text is missing"
            )
        created = moment(item.get("created_ts")) or moment(item.get("created_at"))
        if not created:
            raise TaskError(
                "legacy_open_loop_time_unresolved",
                "legacy creation time cannot be guessed",
            )
        created_at = datetime.fromisoformat(created)
        status = item.get("status") or "待自然延续"
        statuses = {
            "待自然延续": "active",
            "pending": "active",
            "active": "active",
            "open": "active",
            "已完成": "completed",
            "已取消": "completed",
            "completed": "completed",
            "done": "completed",
            "closed": "completed",
            "cancelled": "completed",
            "resolved": "completed",
        }
        if status not in statuses:
            raise TaskError(
                "legacy_open_loop_status_unresolved",
                "legacy item status is unsupported",
            )
        memo_id = "legacy-open-loop-" + digest(
            [plan.lineage_id, owner_key(context), record["person_id"], index]
        )
        expires = created_at + timedelta(days=14)
        memos.append(
            Memo(
                memo_id,
                target,
                1,
                statuses[status],
                item["text"],
                item["text"],
                False,
                None,
                0,
                int(statuses[status] == "completed"),
                context.actor,
                purpose="open_loop",
                expires_at=expires,
                created_at=created_at,
            )
        )
        origins.append({"memo_id": memo_id, "source": item})
    blockers = [
        item
        for item in plan.continuity_blockers
        if item["person_id"] == record["person_id"]
        and item["reason"] != "open_loop_continuity_not_equivalent"
    ]
    policy = dict(record.get("relationship_policy", {}))
    if policy.get("environment_perception_timezone") == "global":
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        evidence_row = store.runtime.read_one(
            "SELECT value FROM runtime_metadata WHERE key=?",
            ("legacy_primary_persona_evidence",),
        )
        try:
            evidence = json.loads(evidence_row["value"]) if evidence_row else {}
            resolved_timezone = evidence.get("environment_perception_timezone")
            if (
                evidence.get("source_config_digest")
                == record.get("source_config_digest")
                and isinstance(resolved_timezone, str)
                and resolved_timezone != "global"
            ):
                ZoneInfo(resolved_timezone)
                policy["resolved_environment_perception_timezone"] = resolved_timezone
                blockers = [
                    item
                    for item in blockers
                    if item["reason"] != "relationship_timezone_unresolved"
                ]
        except (ValueError, TypeError, AttributeError, ZoneInfoNotFoundError):
            pass
    document = {
        "schema": "private.legacy-continuity.v1",
        "authority": "users",
        "owner_key": owner_key(context),
        "source_fingerprint": plan.source_fingerprint,
        "native_route": [
            context.scope.platform,
            context.scope.account_id,
            context.actor.principal.native_account_id,
        ],
        "relationship": {
            field: value
            for field, value in state.items()
            if field.startswith("relationship_")
        },
        "relationship_policy": policy,
        "relationship_interaction": record.get("relationship_interaction", {}),
        "last_activity_at": record.get("last_activity_at"),
        "last_seen": record.get("last_seen"),
        "open_loops": origins,
        "continuity_blockers": blockers,
        "restored_open_loops": len(memos),
        "imported_at": observed_at.isoformat(),
    }
    with store.runtime.transaction() as connection:
        if operator is not None:
            from companion_v2.identity_policy.models import AuthorizationTier

            require(
                agenda.policy.scope_access(
                    operator,
                    scope=context.scope,
                    action="access.policy.set",
                    core_only=True,
                    observed_at=observed_at,
                )
            )
            agenda._authorize(operator, target, "memo.create", observed_at)
        previous = connection.execute(
            "SELECT value FROM runtime_metadata WHERE key=?", (key,)
        ).fetchone()
        if previous:
            prior = json.loads(previous["value"])
            if (
                prior.get("source_fingerprint") != plan.source_fingerprint
                or prior.get("native_route") != document["native_route"]
            ):
                raise TaskError(
                    "legacy_private_continuity_conflict",
                    "existing continuity must be reviewed before replacement",
                )
            if policy.get("resolved_environment_perception_timezone") and not prior.get(
                "relationship_policy", {}
            ).get("resolved_environment_perception_timezone"):
                prior["relationship_policy"][
                    "resolved_environment_perception_timezone"
                ] = policy["resolved_environment_perception_timezone"]
                prior["continuity_blockers"] = [
                    item
                    for item in prior["continuity_blockers"]
                    if item["reason"] != "relationship_timezone_unresolved"
                ]
                if not preview:
                    connection.execute(
                        "UPDATE runtime_metadata SET value=? WHERE key=?",
                        (encode(prior), key),
                    )
            return {
                "restored_open_loops": prior["restored_open_loops"],
                "continuity_blockers": prior["continuity_blockers"],
                "already_present": True,
            }
        if operator is not None:
            # Restore only the old participant's open-item capabilities. This
            # grants no installation tier or AstrBot administrator identity.
            for action in (
                (
                    "memo.create",
                    "memo.list",
                    "memo.read",
                    "memo.edit",
                    "memo.complete",
                    "memo.reopen",
                    "memo.delete",
                )
                if memos
                else ()
            ):
                existing = [
                    grant
                    for grant in agenda.identity.grants_for_subject(
                        context.actor.subject
                    )
                    if grant.scope == context.scope and grant.capability == action
                ]
                allowed = agenda.identity.authorize(
                    context.actor,
                    scope=context.scope,
                    capability=action,
                    observed_at=observed_at,
                )
                if existing and not allowed.is_success:
                    raise TaskError(
                        "legacy_open_loop_permission_conflict",
                        "a later access decision must not be overwritten",
                    )
                if not existing and not preview:
                    agenda.identity.issue_grant(
                        grant_id="legacy-open-loop:" + digest([key, action]),
                        subject=context.actor.subject,
                        scope=context.scope,
                        capability=action,
                        tier=AuthorizationTier.STANDARD,
                        valid_from=observed_at,
                        expected_revision=agenda.identity.revision,
                    )
        else:
            agenda._authorize(context.actor, target, "memo.create", observed_at)
        for memo in memos:
            if agenda.repository.memo(memo.memo_id) is not None:
                raise TaskError(
                    "legacy_private_continuity_conflict",
                    "legacy memo identifier is already used",
                )
            if not preview:
                agenda.repository.save_memo(
                    memo, expected_revision=0, now=memo.created_at
                )
                agenda._register("memo", memo.memo_id, target)
        if not preview:
            connection.execute(
                "INSERT INTO runtime_metadata(key,value) VALUES (?,?)",
                (key, encode(document)),
            )
    return {
        "restored_open_loops": len(memos),
        "continuity_blockers": blockers,
        "already_present": False,
    }


def read_private_continuity(store, context):
    """Read the imported account and current agenda states in its exact scope.

    Args:
        store: The historical store whose writer owns authorization.
        context: The current authenticated private context.

    Returns:
        The relationship account and current open items, or None before import.

    Raises:
        TaskError: The caller no longer owns this private scope.
    """
    from companion_v2.agenda.repository import AgendaRepository
    from companion_v2.memory.models import owner_key

    store.writer.access.check(context)
    key = "legacy_private_continuity:" + owner_key(context)
    row = store.runtime.read_one(
        "SELECT value FROM runtime_metadata WHERE key=?", (key,)
    )
    if row is None:
        return None
    result = json.loads(row["value"])
    if result.get("schema") != "private.legacy-continuity.v1" or result.get(
        "owner_key"
    ) != owner_key(context):
        raise TaskError(
            "legacy_private_continuity_invalid", "continuity ownership is invalid"
        )
    if result.get("native_route") != [
        context.scope.platform,
        context.scope.account_id,
        context.actor.principal.native_account_id,
    ]:
        return None
    repository = AgendaRepository(store.runtime)
    loops = []
    for item in result["open_loops"]:
        memo = repository.memo(item["memo_id"])
        if memo is None or memo.state == "deleted":
            continue
        loops.append(
            {
                **item,
                "status": memo.state,
                "text": memo.body,
                "revision": memo.revision,
                "created_at": memo.created_at.isoformat(),
                "expires_at": memo.expires_at.isoformat(),
                "expired": memo.expired(store.writer.access.tasks.now()),
            }
        )
    result["open_loops"] = loops
    from companion_v2.legacy.legacy_relationship_policy import (
        relationship_stage_for_score,
    )

    result["relationship_stage"] = relationship_stage_for_score(
        result["relationship"].get("relationship_score"),
        result.get("relationship_policy", {}).get("relationship_stage_policy"),
    )
    return result


def update_private_relationship(
    store, context, delta, *, reason_code, event_id, observed_at
):
    """Settle a trusted native event using the preserved v6.5.6 score rules.

    Args:
        store: The private historical-memory store holding the imported account.
        context: The currently authenticated participant and role.
        delta: The change produced by a verified host event, never model input.
        reason_code: The original v6.5.6 event category.
        event_id: Stable native event identifier used for replay protection.
        observed_at: The original event timestamp.

    Returns:
        The original score receipt, or None for an account not yet imported.

    Raises:
        TaskError: The event, authority, schema or preserved policy is invalid.
    """
    from companion_v2.legacy.legacy_relationship_ledger import (
        _finite_number,
        apply_natural_relationship_decay,
        apply_relationship_event,
        clamp_relationship_positive_stage_cap,
    )
    from companion_v2.memory.models import owner_key

    key = "legacy_private_continuity:" + owner_key(context)
    if (
        store.runtime.read_one("SELECT 1 FROM runtime_metadata WHERE key=?", (key,))
        is None
    ):
        return None
    store.writer.access.check(context, at=observed_at)
    if type(delta) is not int or not isinstance(event_id, str) or not event_id.strip():
        raise TaskError(
            "legacy_relationship_event_invalid",
            "native event identity and integer change are required",
        )
    if reason_code not in {
        "inbound",
        "fast_inbound",
        "proactive_reply",
        "fast_proactive_reply",
        "interaction_warmth",
        "fast_interaction_warmth",
        "care_feedback",
        "schedule_adjustment",
        "food_feedback",
        "relationship_violation",
        "relationship_violation_recovery",
    }:
        raise TaskError(
            "legacy_relationship_event_invalid", "event producer is not registered"
        )
    with store.runtime.transaction() as connection:
        row = connection.execute(
            "SELECT value FROM runtime_metadata WHERE key=?", (key,)
        ).fetchone()
        if row is None:
            return None
        document = json.loads(row["value"])
        if document.get("owner_key") != owner_key(context):
            raise TaskError(
                "legacy_private_continuity_invalid", "continuity ownership is invalid"
            )
        if document.get("native_route") != [
            context.scope.platform,
            context.scope.account_id,
            context.actor.principal.native_account_id,
        ]:
            return None
        policy, state = (
            document.get("relationship_policy", {}),
            document["relationship"],
        )
        if policy.get("enable_p4_b_legacy_score_isolation") is True:
            return {
                "changed": False,
                "code": "p4_legacy_score_isolated",
                "score": state.get("relationship_score"),
            }
        if "enable_custom_relationship_stage_policy" not in policy:
            raise TaskError(
                "legacy_relationship_policy_unresolved",
                "legacy relationship master switch is missing",
            )
        if policy["enable_custom_relationship_stage_policy"] is not True:
            return {
                "changed": False,
                "code": "relationship_system_disabled",
                "score": state.get("relationship_score"),
            }
        if state.get("relationship_score_schema_version") != 2:
            raise TaskError(
                "legacy_relationship_schema_unresolved",
                "only reviewed schema-2 accounts may advance",
            )
        timezone_name = policy.get("environment_perception_timezone")
        if timezone_name == "global":
            timezone_name = policy.get("resolved_environment_perception_timezone")
            if not timezone_name:
                raise TaskError(
                    "legacy_relationship_timezone_unresolved",
                    "the original host time zone has not been verified",
                )
        if (
            reason_code in ("relationship_violation", "relationship_violation_recovery")
            and state.get("relationship_role") == "owner"
        ):
            return {
                "changed": False,
                "code": "owner_exempt",
                "score": state.get("relationship_score"),
            }
        violation = state.get("relationship_violation") or {}
        if (
            delta > 0
            and reason_code != "relationship_violation_recovery"
            and state.get("relationship_role") != "owner"
            and isinstance(violation, dict)
            and violation.get("unrecovered_points", 0) > 0
        ):
            return {
                "changed": False,
                "code": "relationship_violation_recovery_pending",
                "score": state.get("relationship_score"),
            }
        state["last_activity_at"] = document.get("last_activity_at")
        state["last_seen"] = document.get("last_seen")
        at = observed_at.timestamp()
        clamp_relationship_positive_stage_cap(
            state,
            cap_key=policy.get("relationship_positive_stage_cap_key", "close"),
            now=at,
        )
        decay = apply_natural_relationship_decay(
            state,
            now=at,
            policy=policy.get("relationship_stage_policy"),
            grace_days=policy.get("relationship_decay_grace_days", 3),
            early_rate=policy.get("relationship_decay_early_per_day", 2),
            middle_rate=policy.get("relationship_decay_middle_per_day", 5),
            late_rate=policy.get("relationship_decay_late_per_day", 8),
            timezone_name=timezone_name,
        )
        options = {
            "event_id": event_id,
            "now": at,
            "positive_daily_cap": policy.get("relationship_positive_daily_cap", 12),
            "event_window_seconds": policy.get("relationship_event_window_minutes", 30)
            * 60,
            "positive_event_cap": policy.get("relationship_positive_event_cap", 4),
            "negative_event_cap": policy.get("relationship_negative_event_cap", 12),
            "positive_stage_cap_key": policy.get(
                "relationship_positive_stage_cap_key", "close"
            ),
            "timezone_name": timezone_name,
        }
        result = apply_relationship_event(
            state, delta, reason_code=reason_code, **options
        )
        if reason_code in ("inbound", "fast_inbound"):
            from companion_v2.legacy.relationship_events import settle_proactive_reply

            reply = settle_proactive_reply(
                connection,
                context,
                document,
                event_id=event_id,
                observed_at=observed_at,
                score_options=options,
            )
            if reply is not None:
                result["proactive_reply"] = reply
        state.pop("last_activity_at", None)
        state.pop("last_seen", None)
        if reason_code in ("inbound", "fast_inbound"):
            # Replayed native events must not move the activity waterline back.
            document["last_activity_at"] = max(
                _finite_number(document.get("last_activity_at")) or 0.0, at
            )
        connection.execute(
            "UPDATE runtime_metadata SET value=? WHERE key=?", (encode(document), key)
        )
    return {**result, "decay": decay}

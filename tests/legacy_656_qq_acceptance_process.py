"""Rehearse exact QQ private history in an isolated host without external calls."""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone
from functools import partial
import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace as NS

import tests.astrbot_data_root  # noqa: F401

from astrbot.core import db_helper
from astrbot.api.web import PluginRequest, bind_request_context
from starlette.requests import Request
from astrbot_host.identity_derivation import derive_bot_id, derive_role_id
from astrbot_host.legacy_api import LegacyMigrationWebAPI
from astrbot_host.legacy_private_memory_api import PrivateMemoryMigrationAPI
from astrbot_host.legacy_group_data_api import LegacyGroupDataWebAPI
from astrbot_host.multi_controller import AstrBotMultiController
from companion_v2.adapters.astrbot.staging import NativeAdapterTypes
from companion_v2.execution.models import digest, encode
from companion_v2.legacy.migration import LegacyMigration
from companion_v2.legacy.group_history import build_group_history_plan
from companion_v2.memory.private_legacy_import import (
    read_private_continuity,
)
from companion_v2.memory.group.access import GroupMemoryAccess
from tests.astrbot_data_root import assert_sdk_database_isolated
from tests.multi_host_support import MultiNativeContext, multi_config
from tests.legacy_identity_acceptance_process import (
    _call,
    _expected_history,
    _query_all,
)
from tests.native_adapter_support import FakeProvider, FakeReverseApi
from tests.test_astrbot_qq_official import OfficialEvent, OfficialPlatform


async def run(snapshot: Path, result_root: Path) -> dict:
    """Validate preserved private state against a frozen native QQ installation.

    Args:
        snapshot: A separately created, read-only source snapshot of AstrBot.
        result_root: A new output directory outside the snapshot and live instance.

    Returns:
        Value-free evidence, including unresolved continuity blockers.

    Raises:
        RuntimeError: Source identity is ambiguous or a verification fails.
    """
    assert_sdk_database_isolated(db_helper)
    snapshot, result_root = snapshot.resolve(strict=True), result_root.resolve()
    if result_root.is_relative_to(snapshot) or result_root.exists():
        raise RuntimeError("a new rehearsal directory outside the snapshot is required")
    result_root.mkdir(parents=True)
    before = {
        str(path.relative_to(snapshot)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in snapshot.rglob("*")
        if path.is_file()
    }
    source = snapshot / "data/plugin_data/astrbot_plugin_private_companion"
    legacy = json.loads((source / "companions.json").read_text(encoding="utf-8-sig"))
    configuration = json.loads(
        (
            snapshot / "data/config/astrbot_plugin_private_companion_config.json"
        ).read_text(encoding="utf-8-sig")
    )
    host = json.loads(
        (snapshot / "data/cmd_config.json").read_text(encoding="utf-8-sig")
    )
    accounts = [
        row
        for row in host["platform"]
        if row.get("enable") and row.get("type") == "qq_official"
    ]
    if len(accounts) != 1 or len(legacy["users"]) != 1:
        raise RuntimeError(
            "this acceptance case requires one verified QQ private target"
        )
    account = str(accounts[0]["id"])
    user = next(iter(legacy["users"].values()))
    participant, umo = user["identity_subject_id"], user["umo"]
    if (
        user["identity_adapter_instance_id"] != account
        or user["identity_platform_kind"] != "qq_official"
        or umo != f"{account}:FriendMessage:{participant}"
        or user["identity_bot_id"] != "unknown_selfid"
    ):
        raise RuntimeError("legacy QQ C2C native identity could not be verified")

    connection = sqlite3.connect(
        (snapshot / "data/data_v4.db").as_uri() + "?mode=ro&immutable=1", uri=True
    )
    connection.execute("PRAGMA query_only=ON")
    try:
        preferences = {
            (scope, scope_id, key): json.loads(value)["val"]
            for scope, scope_id, key, value in connection.execute(
                "SELECT scope,scope_id,key,value FROM preferences"
            )
        }
        routes = preferences[("global", "global", "umop_config_routing")]
        config_id = routes.get(umo, routes.get(f"{account}:*:*"))
        if not isinstance(config_id, str) or Path(config_id).name != config_id:
            raise RuntimeError("native persona configuration route is unavailable")
        selected = json.loads(
            (snapshot / "data/config" / f"abconf_{config_id}.json").read_text(
                encoding="utf-8-sig"
            )
        )
        persona_id = selected["agent_runner"]["config"]["persona"]["persona_id"]
        selected_conversation = preferences[("umo", umo, "sel_conv_id")]
        rows = connection.execute(
            "SELECT conversation_id,persona_id FROM conversations WHERE user_id=? AND conversation_id=?",
            (umo, selected_conversation),
        ).fetchall()
        if len(rows) != 1 or rows[0][1] not in (None, "", persona_id):
            raise RuntimeError("selected private conversation persona is ambiguous")
        if any(key == "session_service_config" for _, _, key in preferences):
            raise RuntimeError(
                "session service overrides require separate verification"
            )
        personas = connection.execute(
            "SELECT persona_id,system_prompt FROM personas WHERE persona_id=?",
            (persona_id,),
        ).fetchall()
        if len(personas) != 1:
            raise RuntimeError("selected native persona is unavailable")
    finally:
        connection.close()

    native = MultiNativeContext()
    native.platform = OfficialPlatform()
    native.platform.metadata = NS(id=account, name="qq_official")
    native.platforms = {account: native.platform}
    native.persona = {"name": persona_id, "prompt": personas[0][1]}
    native.persona_records = {persona_id: native.persona}
    native.persona_manager.personas_v3 = [native.persona]
    native.persona_manager.get_persona_v3_by_id = native.persona_records.get
    native.selections = {account: persona_id}
    native.conversations = {account: selected_conversation}
    native.agent_configuration = selected

    async def conversation(_umo, _cid, *, create_if_not_exists):
        if create_if_not_exists:
            raise RuntimeError("rehearsal must not create native conversations")
        return NS(persona_id=persona_id)

    async def selected_persona(_umo):
        return persona_id

    native.conversation_manager.get_conversation = conversation
    bot_id, role_id = derive_bot_id(account), derive_role_id(account, persona_id)
    group_targets = []
    for index, (group_id, group) in enumerate(legacy.get("groups", {}).items()):
        if group.get("umo") != f"{account}:GroupMessage:{group_id}":
            raise RuntimeError("legacy group route could not be verified")
        group_targets.append(
            {
                "target_id": "source-group-" + str(index),
                "platform_id": account,
                "account_id": account,
                "bot_id": bot_id,
                "group_id": group_id,
            }
        )
    config = multi_config(
        installation_id="legacy-qq-snapshot-acceptance",
        accounts=[{"platform_id": account, "account_id": account, "bot_id": bot_id}],
        targets=[
            {
                "target_id": "source-private",
                "platform_id": account,
                "account_id": account,
                "bot_id": bot_id,
                "participant_id": participant,
            }
        ],
        personas=[{"bot_id": bot_id, "persona_id": persona_id, "role_id": role_id}],
        group_targets=group_targets,
    )
    types = NativeAdapterTypes(
        "4.29.3",
        OfficialEvent,
        FakeProvider,
        NS,
        NS,
        FakeReverseApi,
        session_persona_override_lookup=selected_persona,
    )
    now = datetime.now(timezone.utc)
    controllers = []
    result = {"framework_persona_verified": True, "native_qq_identity_verified": True}
    try:
        for attempt in range(2):
            controller = AstrBotMultiController(
                native,
                config,
                result_root / "runtime",
                native_types=types,
                now=lambda: now,
            )
            controllers.append(controller)
            initialized = await controller.initialize_profile()
            if not initialized.is_success:
                raise RuntimeError("initialize:" + initialized.failure.code)
            started = await controller.start()
            if not started.is_success:
                raise RuntimeError("start:" + started.failure.code)
            context = controller.session.authority.context("source-private", role_id)
            if (
                context.scope.platform,
                context.scope.account_id,
                context.scope.native_scope_id,
            ) != (account, account, participant):
                raise RuntimeError("native QQ scope changed")
            application = controller.session.application
            identity = {
                "bot_id": user["identity_bot_id"],
                "persona_id": persona_id,
                "platform": account,
                "account_id": account,
                "user_id": participant,
                "group_id": "",
            }
            if attempt == 0:
                with controller.runtime.transaction() as database:
                    for key, value in (
                        ("external_memory_owner", "astrbot_plugin_memory_companion"),
                        ("external_memory_identity:" + umo, encode(identity)),
                        (
                            "legacy_primary_persona_evidence",
                            encode(
                                {
                                    "schema": 1,
                                    "source_config_digest": digest(configuration),
                                    "persona_id": persona_id,
                                    "platform_id": account,
                                    "environment_perception_timezone": host["timezone"],
                                }
                            ),
                        ),
                    ):
                        database.execute(
                            "INSERT INTO runtime_metadata(key,value) VALUES (?,?) "
                            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                            (key, value),
                        )
            session = NS(profile=NS(target=context), application=application)
            migration = LegacyMigration(v1_root=source, v1_configuration=configuration)
            legacy_api = LegacyMigrationWebAPI(controller)
            request = PluginRequest(
                Request(
                    {
                        "type": "http",
                        "method": "POST",
                        "path": "/",
                        "headers": [],
                        "query_string": b"",
                    }
                ),
                username="offline-acceptance-admin",
            )
            with bind_request_context(request):
                data_preview = legacy_api._data_plan(migration, source, session, "", "")
                if not data_preview.is_success:
                    raise RuntimeError("profile_plan:" + data_preview.failure.code)
                profiles = await legacy_api._data_apply(
                    migration,
                    source,
                    session,
                    data_preview.unwrap()["plan_token"],
                    source_group_id="",
                    source_persona_id="",
                    import_reactions=False,
                    import_group_network=False,
                    import_journals=False,
                    import_worldbook=False,
                    confirm_member_ids=True,
                    confirm_persona_mapping=True,
                    import_private_contacts=True,
                    import_relationship_profiles=True,
                )
                if not profiles.is_success:
                    raise RuntimeError("profile_apply:" + profiles.failure.code)
                imported_profiles = profiles.unwrap()
            contact_receipt = imported_profiles["private_contacts"]
            if (
                contact_receipt["authorization"]["pending"]
                or contact_receipt["excluded"]
                or contact_receipt["pending"]
                or contact_receipt["created"] + contact_receipt["already_present"] != 1
            ):
                raise RuntimeError("private authorization identity remains unresolved")
            tier = controller.runtime.read_one(
                "SELECT tier FROM subject_access_tiers WHERE subject_kind=? AND subject_id=?",
                (context.actor.subject.kind.value, context.actor.subject.subject_id),
            )
            if user.get("relationship_role") == "owner" and (
                tier is None or tier[0] != "core"
            ):
                raise RuntimeError("private companion owner role was not restored")
            view = NS(
                runtime=controller.runtime,
                now=lambda: now,
                data_root=result_root / "runtime",
                session=session,
                last_session=None,
                _terminated=False,
                _control_lock=asyncio.Lock(),
                controller=controller,
            )
            private_legacy_api = LegacyMigrationWebAPI(view)

            async def fixed_source(_root_override=None, *, session=None):
                return migration, source, "frozen-qq-acceptance-source"

            private_legacy_api._migration = fixed_source
            api = PrivateMemoryMigrationAPI(private_legacy_api)
            prepared = api._plan(migration, source, session)
            if not prepared.is_success:
                raise RuntimeError("private_plan:" + prepared.failure.code)
            document, plan, records, target = prepared.unwrap()
            if document["matched_person_count"] != 1:
                raise RuntimeError("actual_qq_private_identity_unmatched")
            history = await _call(
                api.apply, {"plan_token": document["plan_token"], "confirmed": True}
            )
            continuity = history["continuity"]
            receipt = json.loads(
                controller.runtime.read_one(
                    "SELECT value FROM runtime_metadata WHERE key=?",
                    ("legacy_private_memory_import:" + digest(target),),
                )[0]
            )
            if receipt.get("state") != "completed":
                raise RuntimeError("private migration receipt is incomplete")
            if _query_all(
                application.memory.query.legacy, context
            ) != _expected_history(records):
                raise RuntimeError("private historical content or pagination changed")
            saved = read_private_continuity(application.memory.query.legacy, context)
            expected = {
                key: value
                for key, value in user.items()
                if key.startswith("relationship_")
            }
            if saved["relationship"] != expected:
                raise RuntimeError("relationship account changed during migration")
            if attempt and (
                not history["already_present"] or not continuity["already_present"]
            ):
                raise RuntimeError("private migration is not restart-idempotent")
            result.update(
                {
                    "matched_private_users": len(records),
                    "historical_materials": document["matched_material_count"],
                    "relationship_fields_preserved": len(expected),
                    "ledger_entries_preserved": len(
                        expected.get("relationship_ledger", ())
                    ),
                    "full_history_readback_verified": True,
                    "private_authorization_pending": 0,
                    "private_migration_receipt": receipt["state"],
                    "continuity_blockers": continuity["continuity_blockers"],
                    "private_continuity_ready": not continuity["continuity_blockers"],
                    # Data readback alone cannot approve configuration behavior,
                    # external memory routing or group state evolution.
                    "cutover_ready": False,
                    "cutover_requires_aggregate_gate": True,
                    "verified_after_restart": bool(attempt),
                }
            )
            group_counts = {}
            group_admission_receipts = []
            for configured in group_targets:
                target_group = controller.settings.target(configured["target_id"])
                group_api = LegacyGroupDataWebAPI(
                    controller, target_group.target_id, role_id
                )
                preview = group_api._plan(
                    source, "", target_group.group_id, target_group, controller.session
                )
                imported = group_api.apply_import(
                    {
                        "source_persona_id": "",
                        "source_group_id": target_group.group_id,
                        "plan_token": preview["plan_token"],
                        "confirm_group_mapping": True,
                        "confirm_member_ids": True,
                        "import_group_network": True,
                        "import_group_worldbook": False,
                        "import_group_expression": False,
                        "confirm_expression_mapping": False,
                    },
                    source,
                    target_group,
                    controller.session,
                )
                if imported.get("status") != "ok":
                    raise RuntimeError(
                        "group_apply:"
                        + str(imported.get("data", {}).get("code", "unknown"))
                    )
                admission = imported["data"]["group_admission"]
                if admission["state"] != ("unchanged" if attempt else "imported"):
                    raise RuntimeError(
                        "group admission import is not restart-idempotent"
                    )
                group_admission_receipts.append(
                    {
                        "state": admission["state"],
                        "state_evolution_equivalent": admission[
                            "state_evolution_equivalent"
                        ],
                    }
                )
                event = OfficialEvent(native.platform, group=True, sender=participant)
                event.created_at = now.timestamp()
                event.message_obj.group_id = target_group.group_id
                event.message_obj.group.group_id = target_group.group_id
                event.message_obj.session_id = target_group.group_id
                event.session.platform_name = event.session.platform_id = account
                event.session.session_id = target_group.group_id
                event.unified_msg_origin = (
                    f"{account}:GroupMessage:{target_group.group_id}"
                )
                captured = await controller.session.bridge.groups.capture_text_only(
                    event
                )
                if not captured.is_success:
                    raise RuntimeError("group_capture:" + captured.failure.code)
                group_context = controller.session.authority.groups.select(
                    target_group, captured.unwrap()
                )
                group_plan = build_group_history_plan(
                    source,
                    source_persona_id="",
                    source_group_id=target_group.group_id,
                    configuration=configuration,
                )
                GroupMemoryAccess(application.messages).check(group_context)
                actual_group = _query_all(
                    NS(
                        query=partial(
                            application.memory.query.legacy.query,
                            group_read_authorized=True,
                        )
                    ),
                    group_context,
                )
                expected_group = _expected_history(group_plan.records)
                actual_rows = Counter(
                    {
                        (key[0], key[2], key[3]): count
                        for key, count in actual_group.items()
                    }
                )
                expected_rows = Counter(
                    {
                        (key[0], key[2], key[3]): count
                        for key, count in expected_group.items()
                    }
                )
                if actual_rows != expected_rows:
                    raise RuntimeError("group historical content or scope changed")
                for field, count in group_plan.counts.items():
                    group_counts[field] = group_counts.get(field, 0) + count
            result["group_history_readback"] = {
                "groups": len(group_targets),
                "counts": group_counts,
            }
            result["group_admission_receipts"] = group_admission_receipts
            await controller.terminate()
        result["model_calls"] = len(native.provider.client.state.calls)
        result["platform_sends"] = len(native.platform.sent)
        if result["model_calls"] or result["platform_sends"]:
            raise RuntimeError("rehearsal unexpectedly attempted external activity")
        after = {
            str(path.relative_to(snapshot)): hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
            for path in snapshot.rglob("*")
            if path.is_file()
        }
        result["all_source_files_unchanged"] = before == after
        if before != after:
            raise RuntimeError("source snapshot changed")
        return result
    finally:
        for controller in reversed(controllers):
            await controller.terminate()
        await db_helper.engine.dispose()


def main() -> None:
    """Run the isolated snapshot check and save only value-free results."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--result-dir", type=Path, required=True)
    args = parser.parse_args()
    result = asyncio.run(run(args.snapshot, args.result_dir))
    (args.result_dir / "acceptance.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()

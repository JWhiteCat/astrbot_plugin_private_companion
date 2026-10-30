"""Legacy-read imports preserve QQ identity, history and usable open items."""

from datetime import timedelta
import hashlib
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from companion_v2.agenda.models import AgendaTarget
from companion_v2.agenda.service import AgendaService
from companion_v2.conversation.open_items_context import OpenItemsReader
from companion_v2.execution.models import TaskError, digest
from companion_v2.foundation.refs import OperationRef, VersionRef
from companion_v2.legacy.private_contacts import (
    native_private_identity,
    private_account_matches,
    private_contacts,
)
from companion_v2.legacy.private_memory import SCHEMA, build_private_memory_plan
from companion_v2.memory.private_legacy_import import (
    import_private_continuity,
    import_private_memory,
    read_private_continuity,
    update_private_relationship,
)
from tests.memory_support import MemoryFixture
from tests.test_legacy_private_memory import memory_target


class Legacy656ContinuityTests(MemoryFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        temporary = TemporaryDirectory(prefix="companion-656-continuity-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        participant = self.context.actor.principal.native_account_id
        platform = self.context.scope.platform
        created = self.clock.now() - timedelta(days=1)
        self.user = {
            "identity_subject_id": participant,
            "identity_platform_kind": "qq_official",
            "identity_adapter_instance_id": platform,
            "identity_bot_id": "unknown_selfid",
            "umo": f"{platform}:FriendMessage:{participant}",
            "private_inbound_count": 24,
            "relationship_role": "owner",
            "relationship_score": 24,
            "relationship_score_schema_version": 2,
            "relationship_ledger": [
                {
                    "event_key": str(index),
                    "reason_code": "inbound",
                    "delta": 1,
                    "score_before": index,
                    "score_after": index + 1,
                    "created_at": created.timestamp(),
                }
                for index in range(24)
            ],
            "companion_memory": {
                "items": [{"text": "完整旧记忆", "created_at": created.isoformat()}],
                "updated_at": created.isoformat(),
            },
            "dialogue_episodes": [
                {"date": "2026-10-01", "summary": "完整片段", "avoid_next": "保留字段"}
            ],
            "open_loops": [
                {
                    "text": "面试结果之后继续聊",
                    "status": "待自然延续",
                    "created_ts": created.timestamp(),
                    "created_at": created.isoformat(),
                    "source": "user_message",
                }
            ],
            "behavior_habits": {
                "patterns": [{"text": "保留习惯"}],
                "updated_at": created.isoformat(),
            },
        }
        self.payload = {
            "users": {participant: self.user},
            "_req041_private_memory": {
                "schema": SCHEMA,
                "records": {"partial": {"content": {"open_loops": []}}},
            },
        }
        self.configuration = {
            "basic_config": {
                "enable_custom_relationship_stage_policy": True,
                "environment_perception_timezone": "Asia/Shanghai",
            }
        }
        self.source = self.root / "companions.json"
        self.native_proof = {
            "bot_id": "unknown_selfid",
            "persona_id": "native-role",
            "platform": platform,
            "account_id": self.context.scope.account_id,
            "user_id": participant,
            "group_id": "",
        }
        with self.runtime.transaction() as connection:
            connection.execute(
                "INSERT INTO runtime_metadata(key,value) VALUES (?,?)",
                (
                    "external_memory_identity:" + self.user["umo"],
                    json.dumps(self.native_proof),
                ),
            )
        self.write_source()
        with sqlite3.connect(self.root / "req041_migration_control.db") as connection:
            connection.execute(
                "CREATE TABLE migration_identities(identity_id TEXT,state TEXT,read_generation TEXT)"
            )
            connection.execute(
                "INSERT INTO migration_identities VALUES ('opaque','legacy_read','legacy')"
            )
        connection.close()

    def write_source(self):
        self.source.write_text(
            json.dumps(self.payload, ensure_ascii=False), encoding="utf-8"
        )

    def selected(self):
        plan = build_private_memory_plan(self.root, self.configuration)
        records = plan.matching(
            account_id=self.context.scope.account_id,
            native_actor_id=self.context.actor.principal.native_account_id,
            platform=self.context.scope.platform,
            native_identity=native_private_identity(
                self.runtime,
                self.context.scope.platform,
                self.context.actor.principal.native_account_id,
            ),
        )
        return plan, records

    def test_paused_legacy_users_are_authoritative_and_keep_full_history(self):
        before = hashlib.sha256(self.source.read_bytes()).hexdigest()
        plan, records = self.selected()
        self.assertEqual(plan.authority, "users")
        self.assertEqual(len(records), 1)
        self.assertEqual(
            records[0]["legacy_state"]["relationship_ledger"],
            self.user["relationship_ledger"],
        )
        self.assertIn(
            "companion_memory",
            {item["attributes"]["source_field"] for item in records[0]["materials"]},
        )
        self.assertIn(
            "open_loop_continuity_not_equivalent",
            {row["reason"] for row in plan.continuity_blockers},
        )
        self.assertFalse(plan.document()["cutover_ready"])
        imported = import_private_memory(
            self.recall.legacy, plan, records, memory_target(self.context)
        )
        self.assertFalse(imported["cutover_ready"])
        repeated = import_private_memory(
            self.recall.legacy, plan, records, memory_target(self.context)
        )
        self.assertTrue(repeated["already_present"])
        self.assertIn(
            "完整旧记忆",
            str(
                self.recall.legacy.query(
                    self.context, query_intent="完整旧记忆"
                ).unwrap()
            ),
        )
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), before)

    def test_qq_contact_owner_is_preserved_and_other_platform_never_matches(self):
        plan, records = self.selected()
        self.assertTrue(records)
        self.assertFalse(
            plan.matching(
                account_id=self.context.scope.account_id,
                native_actor_id=self.context.actor.principal.native_account_id,
                platform="another-adapter",
            )
        )
        contacts, rejected = private_contacts(self.payload)
        self.assertFalse(rejected)
        self.assertEqual(contacts[0]["authorization"]["tier"], "core")
        self.assertTrue(
            private_account_matches(self.runtime, self.context, contacts[0])
        )
        self.user["umo"] = (
            "another-adapter:FriendMessage:" + self.user["identity_subject_id"]
        )
        self.write_source()
        self.assertFalse(self.selected()[1])

    def test_qq_unknown_self_id_requires_exact_host_evidence_not_account_equality(self):
        plan, records = self.selected()
        self.assertTrue(records)
        arguments = {
            "account_id": self.context.scope.account_id,
            "native_actor_id": self.context.actor.principal.native_account_id,
            "platform": self.context.scope.platform,
        }
        self.assertFalse(plan.matching(**arguments))
        for key, value in (
            ("bot_id", "other-native-bot"),
            ("account_id", "other-account"),
            ("platform", "other-adapter"),
            ("user_id", "other-user"),
            ("group_id", "group"),
        ):
            with self.subTest(key=key):
                self.assertFalse(
                    plan.matching(
                        **arguments, native_identity={**self.native_proof, key: value}
                    )
                )

    def test_new_generation_or_missing_control_never_silently_uses_users(self):
        with sqlite3.connect(self.root / "req041_migration_control.db") as connection:
            connection.execute("UPDATE migration_identities SET read_generation='new'")
        connection.close()
        self.assertEqual(
            self.selected()[0].source_error,
            "private_memory_mixed_generation_unsupported",
        )
        (self.root / "req041_migration_control.db").unlink()
        self.assertEqual(
            self.selected()[0].source_error, "private_memory_authority_unverified"
        )

    def test_open_loop_is_visible_completable_and_reimport_does_not_revive_it(self):
        plan, records = self.selected()
        result = import_private_continuity(
            self.recall.legacy,
            plan,
            records,
            self.context,
            observed_at=self.clock.now(),
        )
        self.assertEqual(result["restored_open_loops"], 1)
        self.assertNotIn(
            "open_loop_continuity_not_equivalent",
            {row["reason"] for row in result["continuity_blockers"]},
        )
        agenda = AgendaService(self.runtime)
        snapshot = OpenItemsReader(agenda, self.tasks).capture(
            self.context, self.clock.now()
        )
        self.assertEqual(len(snapshot["references"]), 1)
        item = read_private_continuity(self.recall.legacy, self.context)
        self.assertEqual(item["relationship"]["relationship_score"], 24)
        self.assertEqual(len(item["relationship"]["relationship_ledger"]), 24)
        memo = agenda.repository.memo(snapshot["references"][0]["memo_id"])
        self.assertEqual(memo.expires_at - memo.created_at, timedelta(days=14))
        operation = OperationRef(
            "finish-imported-loop",
            "agenda",
            self.context.scope,
            VersionRef("finish-imported-loop", 1),
            "finish-imported-loop",
        )
        finished = agenda.change_memo(
            self.context.actor,
            target=AgendaTarget(
                self.context.scope,
                self.context.role_binding.role,
                self.context.actor.subject,
            ),
            memo_id=memo.memo_id,
            action="complete",
            expected_revision=1,
            expected_role_revision=self.context.role_binding.revision,
            operation=operation,
            observed_at=self.clock.now(),
        )
        self.assertTrue(finished.is_success, finished.failure)
        repeated = import_private_continuity(
            self.recall.legacy,
            plan,
            records,
            self.context,
            observed_at=self.clock.now(),
        )
        self.assertTrue(repeated["already_present"])
        self.assertEqual(
            read_private_continuity(self.recall.legacy, self.context)["open_loops"][0][
                "status"
            ],
            "completed",
        )
        self.assertFalse(
            OpenItemsReader(agenda, self.tasks).capture(self.context, self.clock.now())[
                "references"
            ]
        )
        self.assertFalse(self.runtime.read_all("SELECT * FROM agenda_reminders", ()))

    def test_changed_source_requires_review_and_another_role_cannot_read_state(self):
        plan, records = self.selected()
        import_private_continuity(
            self.recall.legacy,
            plan,
            records,
            self.context,
            observed_at=self.clock.now(),
        )
        other = self.alternate_context(
            bot_id=self.context.scope.bot_id,
            role_id="other-role",
            session_selector=True,
        )
        self.assertIsNone(read_private_continuity(self.recall.legacy, other))
        self.context = self.context_for(self.context.actor, self.context.scope)
        self.user["relationship_score"] = 25
        self.write_source()
        plan, records = self.selected()
        with self.assertRaises(TaskError) as raised:
            import_private_continuity(
                self.recall.legacy,
                plan,
                records,
                self.context,
                observed_at=self.clock.now(),
            )
        self.assertEqual(raised.exception.code, "legacy_private_continuity_conflict")

    def test_native_relationship_event_advances_once_without_rewriting_old_ledger(self):
        plan, records = self.selected()
        import_private_continuity(
            self.recall.legacy,
            plan,
            records,
            self.context,
            observed_at=self.clock.now(),
        )
        result = update_private_relationship(
            self.recall.legacy,
            self.context,
            1,
            reason_code="inbound",
            event_id="native-event-1",
            observed_at=self.clock.now(),
        )
        self.assertEqual((result["code"], result["score"]), ("applied", 25))
        repeated = update_private_relationship(
            self.recall.legacy,
            self.context,
            1,
            reason_code="fast_inbound",
            event_id="native-event-1",
            observed_at=self.clock.now(),
        )
        self.assertEqual(repeated["code"], "duplicate_event")
        state = read_private_continuity(self.recall.legacy, self.context)[
            "relationship"
        ]
        self.assertEqual(
            state["relationship_ledger"][:24], self.user["relationship_ledger"]
        )
        self.assertEqual(len(state["relationship_ledger"]), 25)

    def test_global_timezone_is_blocked_without_matching_source_configuration_proof(
        self,
    ):
        self.configuration["basic_config"]["environment_perception_timezone"] = "global"
        with self.runtime.transaction() as connection:
            connection.execute(
                "INSERT INTO runtime_metadata(key,value) VALUES (?,?)",
                (
                    "legacy_primary_persona_evidence",
                    json.dumps(
                        {
                            "source_config_digest": "wrong-source",
                            "environment_perception_timezone": "Asia/Shanghai",
                        }
                    ),
                ),
            )
        plan, records = self.selected()
        result = import_private_continuity(
            self.recall.legacy,
            plan,
            records,
            self.context,
            observed_at=self.clock.now(),
        )
        self.assertIn(
            "relationship_timezone_unresolved",
            {row["reason"] for row in result["continuity_blockers"]},
        )
        with self.assertRaises(TaskError) as raised:
            update_private_relationship(
                self.recall.legacy,
                self.context,
                1,
                reason_code="inbound",
                event_id="unknown-timezone",
                observed_at=self.clock.now(),
            )
        self.assertEqual(
            raised.exception.code, "legacy_relationship_timezone_unresolved"
        )
        self.assertEqual(
            read_private_continuity(self.recall.legacy, self.context)["relationship"][
                "relationship_score"
            ],
            24,
        )
        with self.runtime.transaction() as connection:
            connection.execute(
                "UPDATE runtime_metadata SET value=? WHERE key=?",
                (
                    json.dumps(
                        {
                            "source_config_digest": digest(self.configuration),
                            "environment_perception_timezone": "Asia/Shanghai",
                        }
                    ),
                    "legacy_primary_persona_evidence",
                ),
            )
        repeated = import_private_continuity(
            self.recall.legacy,
            plan,
            records,
            self.context,
            observed_at=self.clock.now(),
        )
        self.assertTrue(repeated["already_present"])
        self.assertNotIn(
            "relationship_timezone_unresolved",
            {row["reason"] for row in repeated["continuity_blockers"]},
        )
        updated = update_private_relationship(
            self.recall.legacy,
            self.context,
            1,
            reason_code="inbound",
            event_id="verified-timezone",
            observed_at=self.clock.now(),
        )
        self.assertEqual((updated["code"], updated["score"]), ("applied", 25))

    def test_owner_remains_exempt_from_boundary_penalty_and_recovery_events(self):
        plan, records = self.selected()
        import_private_continuity(
            self.recall.legacy,
            plan,
            records,
            self.context,
            observed_at=self.clock.now(),
        )
        before = read_private_continuity(self.recall.legacy, self.context)
        for reason, delta in (
            ("relationship_violation", -4),
            ("relationship_violation_recovery", 4),
        ):
            with self.subTest(reason=reason):
                result = update_private_relationship(
                    self.recall.legacy,
                    self.context,
                    delta,
                    reason_code=reason,
                    event_id=reason,
                    observed_at=self.clock.now(),
                )
                self.assertEqual(
                    result, {"changed": False, "code": "owner_exempt", "score": 24}
                )
                self.assertEqual(
                    read_private_continuity(self.recall.legacy, self.context), before
                )

    def test_imported_pending_reply_settles_original_order_once_even_at_daily_cap(self):
        self.user.update(
            awaiting_reply_since=self.clock.now().timestamp() - 60, reply_count=5
        )
        self.write_source()
        plan, records = self.selected()
        import_private_continuity(
            self.recall.legacy,
            plan,
            records,
            self.context,
            observed_at=self.clock.now(),
        )
        for reason in ("inbound", "fast_inbound"):
            receipt = update_private_relationship(
                self.recall.legacy,
                self.context,
                1,
                reason_code=reason,
                event_id="reply-to-imported-send",
                observed_at=self.clock.now(),
            )
            state = read_private_continuity(self.recall.legacy, self.context)
            self.assertEqual(state["relationship"]["relationship_score"], 27)
            self.assertEqual(state["relationship_interaction"]["reply_count"], 6)
            self.assertEqual(
                state["relationship_interaction"]["awaiting_reply_since"], 0
            )
            if reason == "inbound":
                self.assertEqual(receipt["proactive_reply"]["delta"], 2)
            else:
                self.assertNotIn("proactive_reply", receipt)
        self.assertEqual(
            [
                entry["reason_code"]
                for entry in state["relationship"]["relationship_ledger"][-2:]
            ],
            ["inbound", "proactive_reply"],
        )
        self.assertIn(
            "relationship_progression_not_equivalent",
            {row["reason"] for row in state["continuity_blockers"]},
        )

    def test_capped_imported_reply_clears_the_pending_marker_without_retrying_bonus(
        self,
    ):
        self.configuration["basic_config"]["relationship_positive_daily_cap"] = 1
        self.user.update(
            awaiting_reply_since=self.clock.now().timestamp() - 60, reply_count=5
        )
        self.write_source()
        plan, records = self.selected()
        import_private_continuity(
            self.recall.legacy,
            plan,
            records,
            self.context,
            observed_at=self.clock.now(),
        )
        receipt = update_private_relationship(
            self.recall.legacy,
            self.context,
            1,
            reason_code="inbound",
            event_id="capped-reply",
            observed_at=self.clock.now(),
        )
        self.assertEqual(receipt["proactive_reply"]["code"], "positive_daily_cap")
        state = read_private_continuity(self.recall.legacy, self.context)
        self.assertEqual(state["relationship"]["relationship_score"], 25)
        self.assertEqual(state["relationship_interaction"]["reply_count"], 6)
        self.assertEqual(state["relationship_interaction"]["awaiting_reply_since"], 0)
        repeated = update_private_relationship(
            self.recall.legacy,
            self.context,
            1,
            reason_code="inbound",
            event_id="next-day-input",
            observed_at=self.clock.now() + timedelta(days=1),
        )
        self.assertNotIn("proactive_reply", repeated)

    def test_verified_global_timezone_resets_daily_cap_at_original_local_midnight(self):
        self.configuration["basic_config"]["environment_perception_timezone"] = "global"
        with self.runtime.transaction() as connection:
            connection.execute(
                "INSERT INTO runtime_metadata(key,value) VALUES (?,?)",
                (
                    "legacy_primary_persona_evidence",
                    json.dumps(
                        {
                            "source_config_digest": digest(self.configuration),
                            "environment_perception_timezone": "Asia/Shanghai",
                        }
                    ),
                ),
            )
        plan, records = self.selected()
        imported = import_private_continuity(
            self.recall.legacy,
            plan,
            records,
            self.context,
            observed_at=self.clock.now(),
        )
        self.assertNotIn(
            "relationship_timezone_unresolved",
            {row["reason"] for row in imported["continuity_blockers"]},
        )
        before_midnight = (self.clock.now() + timedelta(days=1)).replace(
            hour=15, minute=59, second=0
        )
        for index in range(3):
            receipt = update_private_relationship(
                self.recall.legacy,
                self.context,
                4,
                reason_code="inbound",
                event_id="local-day-" + str(index),
                observed_at=before_midnight,
            )
            self.assertEqual(receipt["delta"], 4)
        capped = update_private_relationship(
            self.recall.legacy,
            self.context,
            1,
            reason_code="inbound",
            event_id="same-local-day",
            observed_at=before_midnight,
        )
        self.assertEqual(capped["code"], "positive_daily_cap")
        after_midnight = before_midnight + timedelta(minutes=2)
        renewed = update_private_relationship(
            self.recall.legacy,
            self.context,
            1,
            reason_code="inbound",
            event_id="next-local-day",
            observed_at=after_midnight,
        )
        self.assertEqual((renewed["code"], renewed["delta"]), ("applied", 1))


class FrozenRelationshipParityTests(unittest.TestCase):
    def test_rules_match_frozen_656_event_and_decay_vectors(self):
        from copy import deepcopy
        from companion_v2.legacy.legacy_relationship_ledger import (
            apply_natural_relationship_decay,
            apply_relationship_event,
        )

        fixture = json.loads(
            (Path(__file__).parent / "fixtures/legacy_relationship_656.json").read_text(
                encoding="utf-8"
            )
        )
        for vector in fixture["vectors"]:
            for index, case in enumerate(vector["cases"]):
                with self.subTest(
                    kind=vector["kind"], score=vector["score"], index=index
                ):
                    state = deepcopy(case["before"])
                    actual = (
                        apply_relationship_event(
                            state, case["delta"], **case["arguments"]
                        )
                        if vector["kind"] == "events"
                        else apply_natural_relationship_decay(
                            state, **case["arguments"]
                        )
                    )
                    self.assertEqual(actual, case["result"])
                    self.assertEqual(state, case["after"])


if __name__ == "__main__":
    unittest.main()

"""Original reply scoring consumes confirmed candidate deliveries exactly once."""

import tests.astrbot_data_root  # noqa: F401

from datetime import timedelta
import json
import unittest

from companion_v2.configuration.models import ConfigLayer, ConfigTarget
from companion_v2.conversation.reply_settings import GROUP as PRESENTATION_GROUP
from companion_v2.legacy.private_memory import build_private_memory_plan
from companion_v2.memory.private_legacy_import import (
    import_private_continuity,
    read_private_continuity,
    update_private_relationship,
)
from companion_v2.proactive.configuration import target_for
from tests.proactive_support import ContactFixture
from tests.test_role_tool_loop import answered


class Legacy656ProactiveTests(ContactFixture, unittest.IsolatedAsyncioTestCase):
    def import_owner(self, app):
        context = self.contact_context()
        source = self.root / "legacy-source"
        source.mkdir()
        participant = context.actor.principal.native_account_id
        user = {
            "identity_subject_id": participant,
            "identity_platform_kind": "aiocqhttp",
            "identity_adapter_instance_id": context.scope.platform,
            "identity_bot_id": context.scope.account_id,
            "umo": f"{context.scope.platform}:FriendMessage:{participant}",
            "private_inbound_count": 24,
            "relationship_role": "owner",
            "relationship_score": 24,
            "relationship_score_schema_version": 2,
            "relationship_ledger": [],
            "awaiting_reply_since": 0,
            "reply_count": 5,
            "last_activity_at": self.clock.now().timestamp() - 60,
        }
        (source / "companions.json").write_text(
            json.dumps({"users": {participant: user}}), encoding="utf-8"
        )
        plan = build_private_memory_plan(
            source,
            {
                "basic_config": {
                    "enable_custom_relationship_stage_policy": True,
                    "environment_perception_timezone": "UTC",
                }
            },
        )
        records = plan.matching(
            account_id=context.scope.account_id,
            native_actor_id=participant,
            platform=context.scope.platform,
        )
        self.assertEqual(len(records), 1)
        import_private_continuity(
            app.memory.query.legacy,
            plan,
            records,
            context,
            observed_at=self.clock.now(),
            operator=self.core,
        )
        return context

    def inbound(self, app, context, event_id):
        return update_private_relationship(
            app.memory.query.legacy,
            context,
            1,
            reason_code="inbound",
            event_id=event_id,
            observed_at=self.clock.now(),
        )

    async def test_complete_delivery_receipt_survives_restart_and_bonus_is_consumed_once(
        self,
    ):
        app = await self.enabled()
        context = self.import_owner(app)
        task = (await self.contact(app)).unwrap().task
        await self.delivery_state(app, task.task_id, "delivered")
        await app.close()
        app = (
            await self.start(
                self.host(runtime=self.open_runtime()),
                task_generation=1,
                model_generation=1,
            )
        ).application
        self.settings(app, {"enabled": False}).unwrap()
        self.clock.advance(timedelta(seconds=1))
        first_at = self.clock.now()
        first = self.inbound(app, context, "first-native-reply")
        self.assertEqual(first["proactive_reply"]["delta"], 2)
        duplicate = self.inbound(app, context, "first-native-reply")
        self.assertNotIn("proactive_reply", duplicate)
        self.clock.advance(timedelta(seconds=1))
        second = self.inbound(app, context, "second-native-reply")
        self.assertNotIn("proactive_reply", second)
        state = read_private_continuity(app.memory.query.legacy, context)
        self.assertEqual(state["relationship"]["relationship_score"], 28)
        self.assertEqual(state["relationship_interaction"]["reply_count"], 6)
        update_private_relationship(
            app.memory.query.legacy,
            context,
            1,
            reason_code="inbound",
            event_id="first-native-reply",
            observed_at=first_at,
        )
        self.assertEqual(
            read_private_continuity(app.memory.query.legacy, context), state
        )
        self.assertEqual((len(self.called), len(self.sent)), (1, 1))

    async def test_unknown_delivery_cannot_supply_reply_bonus(
        self,
    ):
        app = await self.enabled()
        context = self.import_owner(app)

        async def uncertain(envelope):
            return self.receipt(envelope, "unknown")

        self.send_behavior = uncertain
        task = (await self.contact(app)).unwrap().task
        await self.delivery_state(app, task.task_id, "unknown")
        self.clock.advance(timedelta(seconds=1))
        self.assertNotIn(
            "proactive_reply", self.inbound(app, context, "uncertain-reply")
        )
        state = read_private_continuity(app.memory.query.legacy, context)
        self.assertEqual(state["relationship"]["relationship_score"], 25)
        self.assertEqual(state["relationship_interaction"]["reply_count"], 5)

    async def test_other_native_window_cannot_supply_reply_bonus(self):
        app = await self.enabled()
        context = self.import_owner(app)
        other = self.other_window_context()
        self.settings(app, {"enabled": True}, target=target_for(other)).unwrap()
        self.send_behavior = None
        task = (
            (
                await self.contact(
                    app,
                    self.opportunity("other-contact", scope=other.scope),
                    context=other,
                )
            )
            .unwrap()
            .task
        )
        await self.delivery_state(app, task.task_id, "delivered")
        self.clock.advance(timedelta(seconds=1))
        self.assertNotIn(
            "proactive_reply", self.inbound(app, context, "other-window-reply")
        )
        state = read_private_continuity(app.memory.query.legacy, context)
        self.assertEqual(state["relationship"]["relationship_score"], 25)
        self.assertEqual(state["relationship_interaction"]["reply_count"], 5)

    async def test_partial_segment_delivery_does_not_become_a_completed_contact(self):
        app = await self.enabled()
        context = self.import_owner(app)
        target = ConfigTarget(
            context.scope.installation_id,
            ConfigLayer.WINDOW,
            context.role_binding.role,
            context.scope,
        )
        snapshot = app.configuration.read(
            self.important,
            target=target,
            group_id=PRESENTATION_GROUP,
            observed_at=self.clock.now(),
        ).unwrap()
        changes = {
            "enabled": True,
            "llm_controlled": True,
            "rules_enabled": False,
            "part_gap_ms": 0,
            "content_cleanup_enabled": False,
        }
        app.configuration.patch(
            self.important,
            target=target,
            group_id=PRESENTATION_GROUP,
            expected_edit_token=snapshot.edit_token,
            changes={
                PRESENTATION_GROUP + "." + key: value for key, value in changes.items()
            },
            operation=self.operation(context.scope, owner=PRESENTATION_GROUP),
            observed_at=self.clock.now(),
        ).unwrap()

        async def segmented(_envelope):
            return answered(
                json.dumps(
                    {
                        "decision": "send",
                        "text": "first\n<<PRIVATE_COMPANION_SPLIT>>\nsecond",
                    }
                )
            )

        async def partial(envelope):
            return self.receipt(
                envelope, "delivered" if envelope.sequence == 0 else "unknown"
            )

        self.model_behavior, self.send_behavior = segmented, partial
        task = (await self.contact(app)).unwrap().task
        await self.until(
            lambda: (
                len(app.proactive._delivery_rows(task.task_id)) == 2
                and app.proactive._delivery_rows(task.task_id)[-1]["state"] == "unknown"
            )
        )
        self.assertEqual(
            [row["state"] for row in app.proactive._delivery_rows(task.task_id)],
            ["delivered", "unknown"],
        )
        self.clock.advance(timedelta(seconds=1))
        self.assertNotIn(
            "proactive_reply", self.inbound(app, context, "partial-contact-reply")
        )
        self.assertEqual(
            read_private_continuity(app.memory.query.legacy, context)["relationship"][
                "relationship_score"
            ],
            25,
        )

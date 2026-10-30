"""Exercise imported open items with real host defaults and operator authority."""

from datetime import timedelta
import asyncio
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from astrbot_host.management_identity import dashboard_operator
from companion_v2.agenda.models import AgendaError, AgendaTarget
from companion_v2.agenda.service import AgendaService
from companion_v2.conversation.open_items_context import OpenItemsReader
from companion_v2.foundation.refs import OperationRef, VersionRef
from companion_v2.legacy.private_memory import build_private_memory_plan
from companion_v2.memory.private_legacy_import import (
    import_private_continuity,
    read_private_continuity,
)
from tests.multi_host_support import MultiFixture


class Legacy656HostContinuityTests(MultiFixture, unittest.IsolatedAsyncioTestCase):
    async def test_api_denial_conflict_and_archive_failure_preserve_consistent_state(
        self,
    ):
        from astrbot.api.web import PluginRequest, bind_request_context
        from starlette.requests import Request
        from astrbot_host.legacy_api import LegacyMigrationWebAPI
        from astrbot_host.legacy_private_memory_api import PrivateMemoryMigrationAPI
        from companion_v2.execution.models import TaskError, digest
        from companion_v2.identity_policy.models import AuthorizationTier
        from companion_v2.identity_policy.object_access import ObjectAccessPolicy

        app = await self.start_host()
        context = self.controller.session.authority.context("first", "role-a")
        runtime = self.controller.runtime
        source = self.root / "v1-api"
        source.mkdir()
        user = {
            "identity_platform_kind": "onebot",
            "identity_adapter_instance_id": "qq-main",
            "identity_bot_id": "100",
            "identity_subject_id": "200",
            "private_inbound_count": 1,
            "umo": "qq-main:FriendMessage:200",
            "companion_memory": {"items": ["history-A"]},
            "open_loops": [
                {"text": "open-A", "created_ts": self.clock.now().timestamp()}
            ],
        }
        path = source / "companions.json"
        path.write_text(json.dumps({"users": {"200": user}}), encoding="utf-8")
        session = SimpleNamespace(
            profile=SimpleNamespace(target=context), application=app
        )
        legacy = LegacyMigrationWebAPI(
            SimpleNamespace(
                runtime=runtime,
                session=session,
                last_session=None,
                _terminated=False,
                _control_lock=asyncio.Lock(),
                now=self.clock.now,
            )
        )

        async def migration(*_args, **_kwargs):
            return SimpleNamespace(v1_configuration={}), source, "test"

        legacy._migration = migration
        api = PrivateMemoryMigrationAPI(legacy)

        async def call(handler, body=None):
            async def receive():
                return {
                    "type": "http.request",
                    "body": json.dumps(body or {}).encode(),
                    "more_body": False,
                }

            native = Request(
                {
                    "type": "http",
                    "method": "POST" if body else "GET",
                    "path": "/test",
                    "query_string": b"",
                    "headers": [],
                },
                receive,
            )
            with bind_request_context(PluginRequest(native, username="api-test-admin")):
                return await handler()

        operator = dashboard_operator(runtime, "api-test-admin", at=self.clock.now())
        policy = ObjectAccessPolicy(runtime)
        policy.set_tier(
            operator.subject,
            AuthorizationTier.STANDARD,
            expected_revision=policy.revision,
            observed_at=self.clock.now(),
        )
        planned = (await call(api.plan))["data"]
        body = {"plan_token": planned["plan_token"], "confirmed": True}
        denied = await call(api.apply, body)
        self.assertEqual(denied["status"], "error", denied)
        self.assertEqual(app.memory.query.legacy.query(context).unwrap()["items"], [])
        self.assertIsNone(read_private_continuity(app.memory.query.legacy, context))
        policy.set_tier(
            operator.subject,
            AuthorizationTier.CORE,
            expected_revision=policy.revision,
            observed_at=self.clock.now(),
        )
        with patch(
            "astrbot_host.legacy_private_memory_api.import_private_memory",
            side_effect=TaskError(
                "synthetic_archive_failure", "failed historical write"
            ),
        ):
            failed = await call(api.apply, body)
        self.assertEqual(failed["data"]["code"], "synthetic_archive_failure")
        self.assertIsNone(read_private_continuity(app.memory.query.legacy, context))
        self.assertEqual(app.memory.query.legacy.query(context).unwrap()["items"], [])
        target = legacy._memory_target(context)
        marker = json.loads(
            runtime.read_one(
                "SELECT value FROM runtime_metadata WHERE key=?",
                ("legacy_private_memory_import:" + digest(target),),
            )["value"]
        )
        self.assertEqual((marker["state"], marker["cutover_ready"]), ("failed", False))
        imported = await call(api.apply, body)
        self.assertEqual(imported["status"], "ok", imported)
        history_a = app.memory.query.legacy.query(context).unwrap()["items"]
        continuity_a = read_private_continuity(app.memory.query.legacy, context)
        user["companion_memory"] = {"items": ["history-B"]}
        path.write_text(json.dumps({"users": {"200": user}}), encoding="utf-8")
        stale = await call(api.apply, body)
        self.assertEqual(stale["data"]["code"], "legacy_plan_stale")
        new_plan = (await call(api.plan))["data"]
        conflict = await call(api.apply, {**body, "plan_token": new_plan["plan_token"]})
        self.assertEqual(conflict["data"]["code"], "legacy_private_continuity_conflict")
        self.assertEqual(
            app.memory.query.legacy.query(context).unwrap()["items"], history_a
        )
        self.assertEqual(
            read_private_continuity(app.memory.query.legacy, context), continuity_a
        )

    async def test_dashboard_import_restores_only_target_memo_capabilities(self):
        app = await self.start_host()
        context = self.controller.session.authority.context("first", "role-a")
        runtime = self.controller.runtime
        source = self.root / "v1"
        source.mkdir()
        created = self.clock.now() - timedelta(days=1)
        source.joinpath("companions.json").write_text(
            json.dumps(
                {
                    "users": {
                        "200": {
                            "identity_platform_kind": "onebot",
                            "identity_adapter_instance_id": "qq-main",
                            "identity_bot_id": "100",
                            "identity_subject_id": "200",
                            "private_inbound_count": 1,
                            "umo": "qq-main:FriendMessage:200",
                            "relationship_role": "owner",
                            "open_loops": [
                                {
                                    "text": "继续问面试结果",
                                    "status": "待自然延续",
                                    "created_ts": created.timestamp(),
                                }
                            ],
                        }
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        plan = build_private_memory_plan(source)
        records = plan.matching(
            account_id="100", native_actor_id="200", platform="qq-main"
        )
        self.assertEqual(len(records), 1)
        agenda = AgendaService(runtime)
        before_tiers = [
            dict(row)
            for row in runtime.read_all("SELECT * FROM subject_access_tiers", ())
        ]
        self.assertFalse(
            agenda.identity.authorize(
                context.actor,
                scope=context.scope,
                capability="memo.create",
                observed_at=self.clock.now(),
            ).is_success
        )
        with self.assertRaises(AgendaError):
            import_private_continuity(
                app.memory.query.legacy,
                plan,
                records,
                context,
                observed_at=self.clock.now(),
            )
        self.assertIsNone(read_private_continuity(app.memory.query.legacy, context))
        operator = dashboard_operator(
            runtime, "host-authenticated-admin", at=self.clock.now()
        )
        tier_after_operator = [
            dict(row)
            for row in runtime.read_all("SELECT * FROM subject_access_tiers", ())
        ]
        result = import_private_continuity(
            app.memory.query.legacy,
            plan,
            records,
            context,
            observed_at=self.clock.now(),
            operator=operator,
        )
        self.assertEqual(result["restored_open_loops"], 1)
        self.assertEqual(
            [
                dict(row)
                for row in runtime.read_all("SELECT * FROM subject_access_tiers", ())
            ],
            tier_after_operator,
        )
        self.assertEqual(
            [row for row in tier_after_operator if row in before_tiers], before_tiers
        )
        restored = [
            grant
            for grant in agenda.identity.grants_for_subject(context.actor.subject)
            if grant.grant_id.startswith("legacy-open-loop:")
        ]
        self.assertEqual(
            {grant.capability for grant in restored},
            {
                "memo.create",
                "memo.list",
                "memo.read",
                "memo.edit",
                "memo.complete",
                "memo.reopen",
                "memo.delete",
            },
        )
        self.assertTrue(
            all(
                grant.scope == context.scope and grant.tier.value == "standard"
                for grant in restored
            )
        )
        items = OpenItemsReader(agenda, app.tasks).capture(context, self.clock.now())
        self.assertEqual(len(items["references"]), 1)
        memo = agenda.repository.memo(items["references"][0]["memo_id"])
        operation = OperationRef(
            "host-complete-legacy",
            "agenda",
            context.scope,
            VersionRef("host-complete-legacy", 1),
            "host-complete-legacy",
        )
        finished = agenda.change_memo(
            context.actor,
            target=AgendaTarget(
                context.scope, context.role_binding.role, context.actor.subject
            ),
            memo_id=memo.memo_id,
            action="complete",
            expected_revision=1,
            expected_role_revision=context.role_binding.revision,
            operation=operation,
            observed_at=self.clock.now(),
        )
        self.assertTrue(finished.is_success, finished.failure)
        self.assertFalse(
            OpenItemsReader(agenda, app.tasks).capture(context, self.clock.now())[
                "references"
            ]
        )
        other = self.controller.session.authority.context("second", "role-a")
        self.assertIsNone(read_private_continuity(app.memory.query.legacy, other))
        self.assertFalse(
            agenda.identity.authorize(
                other.actor,
                scope=other.scope,
                capability="memo.read",
                observed_at=self.clock.now(),
            ).is_success
        )
        self.assertEqual(self.native.provider.client.state.calls, [])


if __name__ == "__main__":
    unittest.main()

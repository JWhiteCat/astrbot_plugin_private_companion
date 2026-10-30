"""Exercise explicit V1 group mappings through registered host routes."""

from __future__ import annotations

import json
import unittest
from urllib.parse import urlencode
from unittest.mock import patch

from astrbot.api.web import PluginRequest, bind_request_context
from starlette.requests import Request

from astrbot_host.legacy_group_data_api import LegacyGroupDataWebAPI
from astrbot_host.legacy_api import LegacyMigrationWebAPI
from astrbot_host.multi_api import PREFIX as HOST_PREFIX
from companion_v2.execution.models import TaskError, digest
from companion_v2.foundation.refs import ScopeKind, ScopeRef
from companion_v2.knowledge.catalog import KnowledgeCatalog, SourceBinding
from companion_v2.knowledge.retrieval import KnowledgeQuery, KnowledgeRetriever
from companion_v2.relationships.group_network import GroupRelationshipStore
from tests.group_host_support import GroupFixture


class LegacyGroupDataHostTests(GroupFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.controller.register_routes()
        self.routes = {(path, methods[0]): handler
                       for path, handler, methods, _ in self.native.registered_web_apis}

    def _path(self, target="group-a", role="role-a"):
        return (HOST_PREFIX + "/targets/" + target + "/roles/" + digest(role)
                + "/legacy-group-data")

    async def request(self, suffix="/plan", *, target="group-a", role="role-a", username="admin",
                      data=None, query=""):
        path = self._path(target, role) + suffix
        body = json.dumps(data, ensure_ascii=False).encode("utf-8") if data is not None else b""
        method = "POST" if data is not None else "GET"

        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        native = Request({"type": "http", "method": method, "path": path, "headers": [],
                          "query_string": query.encode("utf-8")}, receive=receive)
        with bind_request_context(PluginRequest(native, username=username)):
            return await self.routes[(path, method)]()

    def source_root(self):
        root = self.root / "synthetic-v1"
        root.mkdir()
        payload = {
            "groups": {"789": {"members": {
                "200": {"name": "甲群成员", "count": 8},
                "201": {"name": "乙群成员", "count": 4}},
                "relationship_edges": {"200|201": {
                    "a": "200", "b": "201", "count": 2, "tone": "玩笑"}}}},
            "worldbook_entries": [{"id": "group-entry", "name": "甲群资料",
                "content": "只有明确映射到甲群后才可注入的合成资料。",
                "__template_key": "group", "scope": ["default:groupmessage:789"]}],
            "worldbook_group_profiles": {"789": {"name": "甲群补充资料",
                "content": "仅用于验证群范围的第二条合成资料。"}},
            "worldbook_member_profiles": {"200": {"user_id": "200", "name": "甲群成员",
                "content": "甲群合成成员线索。", "projection_kind": "group_observation",
                "group_observation_scope_ids": ["default:groupmessage:789"]}},
        }
        (root / "companions.json").write_text(
            json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
        return root

    async def plan(self, root, *, target="group-a", role="role-a"):
        # Match the setup page: empty primary-persona values are not sent.
        query = urlencode({"v1_root": str(root), "source_group_id": "789"})
        response = await self.request("/plan", target=target, role=role, query=query)
        self.assertEqual(response["status"], "ok", response)
        return response["data"]

    async def apply(self, plan, root, *, target="group-a", role="role-a"):
        return await self.request("/apply", target=target, role=role, data={
            "plan_token": plan["plan_token"], "source_persona_id": "", "source_group_id": "789",
            "import_group_network": True, "import_group_worldbook": True,
            "confirm_member_ids": True, "confirm_group_mapping": True, "v1_root": str(root),
        })

    def scope(self, *, platform, bot, account):
        return ScopeRef(self.controller.runtime.installation_id, platform, ScopeKind.GROUP,
            "300", bot, account, "300", "300")

    def group_binding(self, *, platform, bot, account):
        return SourceBinding(self.controller.runtime.installation_id, role_id="role-a", persona_id="role-a",
            native_scope_ids=("300",), scope_kind=ScopeKind.GROUP,
            scope_route=(platform, bot, account))

    async def test_primary_persona_preview_accepts_omitted_id_but_apply_requires_it(self):
        root = self.source_root()
        await self.start_host()
        plan = await self.plan(root)
        explicit = await self.request("/plan", query=urlencode({
            "v1_root": str(root), "source_persona_id": "", "source_group_id": "789"}))
        self.assertEqual(explicit["status"], "ok", explicit)
        self.assertEqual(plan["source_persona_id"], "")
        self.assertEqual(plan["plan_token"], explicit["data"]["plan_token"])
        missing = await self.request("/apply", data={
            "plan_token": plan["plan_token"], "source_group_id": "789",
            "import_group_network": True, "import_group_worldbook": True,
            "confirm_member_ids": True, "confirm_group_mapping": True,
            "v1_root": str(root)})
        self.assertEqual(missing["status"], "error", missing)
        self.assertEqual(missing["data"]["code"], "legacy_request_invalid")

    async def test_mapping_import_is_explicit_and_isolated_across_same_id_platform_groups(self):
        root = self.source_root()
        await self.start_host()
        plan = await self.plan(root)

        self.assertEqual(plan["mapping_policy"], {
            "source_to_targets": "one_to_many_explicit", "targets_per_plan": 1,
            "automatic_fanout": False, "source_platform_id": None, "source_platform_known": False})
        self.assertEqual(plan["target"], {"target_id": "group-a", "platform_id": "qq-main",
            "account_id": "100", "bot_id": "bot-a", "group_id": "300", "role_id": "role-a"})
        result = await self.apply(plan, root)
        self.assertEqual(result["status"], "ok", result)
        self.assertTrue(result["data"]["group_network"]["imported"])

        runtime = self.controller.runtime
        store = GroupRelationshipStore(runtime)
        selected = store.projection_for_route(runtime.installation_id, "qq-main", "bot-a", "100", "300", "role-a")
        other_platform = store.projection_for_route(runtime.installation_id, "qq-second", "bot-b", "101", "300", "role-a")
        self.assertEqual({item["native_account_id"] for item in selected["members"]}, {"200", "201"})
        self.assertEqual(other_platform["members"], [])
        self.assertEqual(len(selected["edges"]), 1)
        self.assertEqual(len(other_platform["edges"]), 0)

        catalog = KnowledgeCatalog(runtime)
        group_id = LegacyMigrationWebAPI._worldbook_source_id("", "789")
        version = catalog.get(self.group_binding(platform="qq-main", bot="bot-a", account="100"), group_id)
        self.assertIsNotNone(version)
        self.assertEqual(version.binding.scope_route, ("qq-main", "bot-a", "100"))
        self.assertEqual(version.members[0].group_ids, ("300",))
        candidates = catalog.background_candidates(installation_id=runtime.installation_id,
            source_limit=64, char_budget=4000)
        retriever = KnowledgeRetriever(candidates)
        query = "明确映射到甲群后才可注入"
        selected_result = retriever.select(KnowledgeQuery(query,
            self.scope(platform="qq-main", bot="bot-a", account="100"), role_id="role-a"))
        self.assertTrue(selected_result.hits)
        other_routes = (
            ("qq-second", "bot-a", "100"),
            ("qq-main", "bot-b", "100"),
            ("qq-main", "bot-a", "101"),
        )
        for platform, bot, account in other_routes:
            with self.subTest(platform=platform, bot=bot, account=account):
                foreign_scope = self.scope(platform=platform, bot=bot, account=account)
                foreign_result = retriever.select(KnowledgeQuery(query, foreign_scope, role_id="role-a"))
                self.assertEqual(foreign_result.hits, ())
                self.assertEqual(catalog.member_entries(installation_id=runtime.installation_id,
                    source_limit=64, role_id="role-a", scope_ids=("300",), scope_kind=ScopeKind.GROUP,
                    scope=foreign_scope), ())

        self.assertEqual(len(catalog.member_entries(installation_id=runtime.installation_id,
            source_limit=64, role_id="role-a", scope_ids=("300",), scope_kind=ScopeKind.GROUP,
            scope=self.scope(platform="qq-main", bot="bot-a", account="100"))), 1)

    async def test_worldbook_failure_rolls_back_relationship_write(self):
        root = self.source_root()
        await self.start_host()
        plan = await self.plan(root)

        def fail_save(_self, *_args, **_kwargs):
            raise TaskError("test_worldbook_write_failed", "synthetic failure")

        with patch.object(KnowledgeCatalog, "save", autospec=True, side_effect=fail_save):
            response = await self.apply(plan, root)

        self.assertEqual(response["status"], "error", response)
        self.assertEqual(response["data"]["code"], "test_worldbook_write_failed")
        runtime = self.controller.runtime
        network = GroupRelationshipStore(runtime).projection_for_route(
            runtime.installation_id, "qq-main", "bot-a", "100", "300", "role-a")
        self.assertEqual(network["members"], [])
        self.assertIsNone(KnowledgeCatalog(runtime).get(
            self.group_binding(platform="qq-main", bot="bot-a", account="100"),
            LegacyMigrationWebAPI._worldbook_source_id("", "789")))

    async def test_plan_token_is_stale_after_target_graph_changes(self):
        root = self.source_root()
        await self.start_host()
        plan = await self.plan(root)
        target = self.controller.settings.target("group-a")
        context = LegacyGroupDataWebAPI(self.controller, target.target_id, "role-a")._scope_context(target)
        self.controller.session.application.messages.group_network.import_legacy_aggregate(
            context, source_key="independent-change", members={"299": {"name": "另一位",
                "message_count": 1, "active_days": 1}}, edges=[])

        response = await self.apply(plan, root)
        self.assertEqual(response["status"], "error", response)
        self.assertEqual(response["data"]["code"], "legacy_plan_stale")
        network = GroupRelationshipStore(self.controller.runtime).projection_for_route(
            self.controller.runtime.installation_id, "qq-main", "bot-a", "100", "300", "role-a")
        self.assertEqual({item["native_account_id"] for item in network["members"]}, {"299"})

    async def test_group_expression_import_keeps_rules_in_exact_group(self):
        root = self.source_root()
        path = root / "companions.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["groups"]["300"] = {"expression_profile": {
            "learned_rules": [{"id": "group-greeting", "kind": "style",
                "situation": "群友进来打招呼时", "pattern": "早呀",
                "instruction": "轻快接话", "channels": ["group"],
                "evidence_examples": ["早呀"]}],
            "pending_samples": [{"id": "sample-1", "text": "早呀",
                "ts": self.controller.now().timestamp()}]}}
        path.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
        await self.start_host()
        query = urlencode({"v1_root": str(root), "source_group_id": "300"})
        planned = await self.request("/plan", query=query)
        self.assertEqual(planned["status"], "ok", planned)
        plan = planned["data"]
        self.assertEqual(plan["group_expression"]["adopted_count"], 1)
        request_data = {"plan_token": plan["plan_token"], "source_persona_id": "",
            "source_group_id": "300", "import_group_network": False,
            "import_group_worldbook": False, "import_group_expression": True,
            "confirm_member_ids": False, "confirm_group_mapping": True,
            "confirm_expression_mapping": False, "v1_root": str(root)}
        denied = await self.request("/apply", data=request_data)
        self.assertEqual(denied["data"]["code"], "legacy_expression_mapping_confirmation_required")
        imported = await self.request("/apply", data={**request_data,
            "confirm_expression_mapping": True})
        self.assertEqual(imported["status"], "ok", imported)
        self.assertEqual(imported["data"]["group_expression"]["created"], 1)
        replay_plan = (await self.request("/plan", query=query))["data"]
        self.assertEqual(replay_plan["group_expression"]["remaining"], 0)
        rows = self.controller.runtime.read_all(
            "SELECT state, scope_key FROM expression_style_signals WHERE run_id LIKE 'legacy-expression:%'", ())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["state"], "adopted")
        context = LegacyGroupDataWebAPI(self.controller, "group-a", "role-a")._scope_context(
            self.controller.settings.target("group-a"))
        self.assertEqual(rows[0]["scope_key"], digest(context.scope))

    async def test_group_history_is_complete_paged_queryable_and_scope_isolated(self):
        from astrbot_host.group_authority import GroupAuthority
        from companion_v2.legacy.private_memory import canonical
        from tests.test_memory_ownership import external

        root = self.source_root()
        path = root / "companions.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        records = {"recent_messages": [{"text": "完整长消息" + "甲乙丙丁" * 15000,
            "sender_id": "opaque_QQ-openid", "ts": self.clock.now().timestamp(), "message_id": "old-native-id"}],
            "group_episodes": [{"summary": "旧群聊片段", "date": "2026-01-01", "avoid_next": ["保留状态"]}],
            "topic_threads": [{"signature": "old-topic", "title": "原话题", "bot_joined": True,
                "started_ts": self.clock.now().timestamp(), "recent_examples": ["原例子"]}]}
        payload["groups"]["789"].update(records)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        before = path.read_bytes()
        app = await self.start_host()
        plan = await self.plan(root)
        self.assertEqual(plan["group_history"]["counts"], {key: 1 for key in records})
        imported = await self.apply(plan, root)
        self.assertEqual(imported["status"], "ok", imported)
        self.assertFalse(imported["data"]["group_history"]["already_present"])
        repeated = await self.apply(await self.plan(root), root)
        self.assertTrue(repeated["data"]["group_history"]["already_present"])
        app.memory.owner = external()
        captured = (await self.controller.session.bridge.capture(self.group_event())).unwrap()
        context = GroupAuthority(self.controller.session.authority).select(
            self.controller.settings.target("group-a"), captured)
        self.assertEqual([tool.action for tool in app.tools.group_memory.catalog_for(context)],
                         ["group.memory.legacy.query"])
        chunks, cursor = {}, None
        for _ in range(100):
            arguments = {"query": "", **({"cursor": cursor} if cursor else {})}
            result, operation = app.tools.group_memory.execute(context, "read-only", "history",
                "group.memory.legacy.query", arguments)
            self.assertIsNone(operation)
            validated, materials = app.tools.group_memory.validate({"batch_id": "read-only", "sequence": 1,
                "action": "group.memory.legacy.query", "state": "succeeded"}, context, result)
            self.assertEqual(validated, result)
            self.assertIsNone(materials)
            page = result["payload"]
            for item in page["items"]:
                self.assertFalse(item["current_fact"])
                key = item["source_field"], item["source_index"]
                chunks.setdefault(key, []).append((item["source_offset"] + item["excerpt_start"], item["text"]))
            cursor = page["next_cursor"]
            if cursor is None:
                break
        self.assertIsNone(cursor, "all original characters must be reachable through bounded pages")
        self.assertEqual({key: "".join(text for _, text in sorted(parts)) for key, parts in chunks.items()},
                         {(key, 0): canonical(value[0]) for key, value in records.items()})
        for target_id in ("group-b", "group-c"):
            other_capture = (await self.controller.session.bridge.capture(self.group_event(target_id))).unwrap()
            other = GroupAuthority(self.controller.session.authority).select(
                self.controller.settings.target(target_id), other_capture)
            result, _ = app.tools.group_memory.execute(other, "read-only", "other-history",
                "group.memory.legacy.query", {"query": ""})
            self.assertEqual(result["payload"]["items"], [])
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.native.provider.client.state.calls, [])
        for field in records:
            payload["groups"]["789"][field] = []
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        cleared = await self.apply(await self.plan(root), root)
        self.assertEqual(cleared["status"], "ok", cleared)
        self.assertEqual(cleared["data"]["group_history"]["imported"], {"memories": 0, "timeline": 0})
        self.assertFalse(cleared["data"]["group_history"]["already_present"])
        empty, _ = app.tools.group_memory.execute(context, "read-only", "cleared-history",
            "group.memory.legacy.query", {"query": ""})
        self.assertEqual(empty["payload"]["items"], [])
        replay = await self.apply(await self.plan(root), root)
        self.assertTrue(replay["data"]["group_history"]["already_present"])

    async def test_changed_group_history_invalidates_review_and_malformed_history_blocks(self):
        root = self.source_root()
        await self.start_host()
        plan = await self.plan(root)
        path = root / "companions.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["groups"]["789"]["recent_messages"] = [{"text": "changed after preview"}]
        path.write_text(json.dumps(payload), encoding="utf-8")
        stale = await self.apply(plan, root)
        self.assertEqual(stale["data"]["code"], "legacy_plan_stale")
        payload["groups"]["789"]["recent_messages"] = "malformed collection"
        path.write_text(json.dumps(payload), encoding="utf-8")
        current = await self.plan(root)
        self.assertTrue(current["group_history"]["rejected"])
        rejected = await self.apply(current, root)
        self.assertEqual(rejected["data"]["code"], "legacy_group_history_source_invalid")


if __name__ == "__main__":
    unittest.main()

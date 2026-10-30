"""External memory retains native identity, authorization and sole ownership."""

from tests import astrbot_data_root as _isolated_astrbot_root  # noqa: F401

import importlib
from dataclasses import replace
import json
import os
from pathlib import Path
import sqlite3
import sys
from types import ModuleType, SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock

from astrbot_host.memory_linkage import read_external_memory_summary
from companion_v2.adapters.astrbot.memory_owner import (
    EXTERNAL_MEMORY_IDENTITY_PREFIX,
    EXTERNAL_MEMORY_OWNER_KEY,
    MEMORY_OWNER_PLUGIN_ID,
    memory_owner_provider,
    native_memory_identity,
    record_native_memory_identity,
)
from companion_v2.adapters.astrbot.memory_recall import with_external_memory
from companion_v2.execution.models import TaskError
from companion_v2.foundation.refs import ScopeKind
from tests.native_adapter_support import NativeFixture


class NativeMemoryIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_recording_uses_observed_owner_and_refuses_another_route(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.execute(
            "CREATE TABLE runtime_metadata (key TEXT PRIMARY KEY, value TEXT)"
        )
        connection.execute(
            "INSERT INTO runtime_metadata VALUES (?, ?)",
            (EXTERNAL_MEMORY_OWNER_KEY, MEMORY_OWNER_PLUGIN_ID),
        )
        runtime = NS(
            read_one=lambda sql, args: connection.execute(sql, args).fetchone(),
            transaction=lambda: connection,
        )
        scope = NS(
            kind=ScopeKind.PRIVATE,
            platform="AstrBot",
            account_id="AstrBot",
            native_scope_id="user-openid",
        )
        native = NS(get_all_stars=lambda: [])
        bridge = NS(
            context=native,
            verified_persona=AsyncMock(return_value={"persona_id": "original"}),
        )
        event = NS(
            unified_msg_origin="AstrBot:FriendMessage:user-openid",
            get_self_id=lambda: "unknown_selfid",
        )
        context = NS(scope=scope, role_binding=object())
        self.assertTrue(
            await record_native_memory_identity(runtime, bridge, event, context)
        )
        row = runtime.read_one(
            "SELECT value FROM runtime_metadata WHERE key=?",
            (EXTERNAL_MEMORY_IDENTITY_PREFIX + event.unified_msg_origin,),
        )
        proof = json.loads(row[0])
        self.assertEqual(
            proof,
            {
                "bot_id": "unknown_selfid",
                "persona_id": "original",
                "platform": "AstrBot",
                "account_id": "AstrBot",
                "user_id": "user-openid",
                "group_id": "",
            },
        )
        event.unified_msg_origin = "AstrBot:FriendMessage:someone-else"
        self.assertFalse(
            await record_native_memory_identity(runtime, bridge, event, context)
        )
        self.assertEqual(
            connection.execute("SELECT COUNT(*) FROM runtime_metadata").fetchone()[0], 2
        )

    def test_official_instance_and_app_id_cannot_substitute_for_observed_owner(self):
        scope = NS(
            kind=ScopeKind.PRIVATE,
            platform="qq-instance",
            native_scope_id="user-openid",
            account_id="qq-instance",
            bot_id="logical-bot",
        )
        context = NS(
            get_platform_inst=lambda _: NS(
                meta=lambda: NS(name="qq_official"), appid="app-id"
            )
        )
        with self.assertRaisesRegex(TaskError, "native memory bot identity"):
            native_memory_identity(context, scope, persona_id="original")
        recorded = {
            "bot_id": "unknown_selfid",
            "persona_id": "original",
            "platform": scope.platform,
            "account_id": scope.account_id,
            "user_id": scope.native_scope_id,
            "group_id": "",
        }
        reads = []

        def read(sql, args):
            reads.append(args)
            return (json.dumps(recorded),)

        identity = native_memory_identity(
            context, scope, persona_id="original", runtime=NS(read_one=read)
        )
        self.assertEqual(identity["bot_id"], "unknown_selfid")
        self.assertEqual(
            identity["session_id"], "qq-instance:FriendMessage:user-openid"
        )
        self.assertEqual(
            reads, [(EXTERNAL_MEMORY_IDENTITY_PREFIX + identity["session_id"],)]
        )
        with self.assertRaises(TaskError):
            native_memory_identity(
                context, scope, persona_id="another-persona", runtime=NS(read_one=read)
            )
        for field in ("platform", "account_id", "user_id", "group_id"):
            original = recorded[field]
            recorded[field] = "different"
            with self.assertRaises(TaskError):
                native_memory_identity(
                    context, scope, persona_id="original", runtime=NS(read_one=read)
                )
            recorded[field] = original

    def test_persistent_external_selection_does_not_fall_back_when_plugin_is_down(self):
        context = NS(get_all_stars=lambda: [])
        runtime = NS(
            read_one=lambda sql, args: (
                (MEMORY_OWNER_PLUGIN_ID,)
                if args == (EXTERNAL_MEMORY_OWNER_KEY,)
                else None
            )
        )
        self.assertTrue(
            memory_owner_provider(context, runtime=runtime).state().delegated
        )
        self.assertFalse(memory_owner_provider(context).state().delegated)


class ExternalRecallTests(NativeFixture, unittest.IsolatedAsyncioTestCase):
    async def test_enabled_p5_gate_blocks_transport_instead_of_becoming_empty_recall(
        self,
    ):
        compose = AsyncMock(return_value="")
        owner = NS(
            bridge_lifecycle_status=lambda: {"active": True},
            compose_context=compose,
            p5_capability_status=lambda: {"bridge_gate": True},
        )
        self.native.get_all_stars = lambda: [
            NS(
                name="MemoryCompanion",
                activated=True,
                module=NS(get_memory_companion_bridge=lambda: owner),
            )
        ]
        result = await self.native_model.generate(await self.envelope())
        self.assertEqual(result.status, "not_started")
        self.assertEqual(
            result.diagnostics.error_code, "external_memory_attestation_required"
        )
        compose.assert_not_awaited()
        self.assertEqual(self.native.provider.client.state.calls, [])

    async def test_native_transport_receives_scoped_external_recall_without_mutating_original(
        self,
    ):
        calls = []

        async def compose(**arguments):
            calls.append(arguments)
            return "Verified external memory: the user avoids coffee."

        owner = NS(
            bridge_lifecycle_status=lambda: {"active": True}, compose_context=compose
        )
        module = NS(get_memory_companion_bridge=lambda: owner)
        self.native.get_all_stars = lambda: [
            NS(name="MemoryCompanion", activated=True, module=module)
        ]
        envelope = await self.envelope()
        result = await self.native_model.generate(envelope)
        self.assertEqual(result.status, "generated")
        identity = calls[0]["session_context"]
        self.assertEqual(identity["session_id"], "qq-main:FriendMessage:200")
        self.assertEqual(identity["bot_id"], "100")
        self.assertEqual(identity["persona_id"], "original")
        self.assertNotIn(
            "Verified external memory", str(envelope.original.context.materials)
        )
        payload = self.native.provider.client.state.calls[-1][0]
        self.assertIn("Verified external memory", json.dumps(payload))

    async def test_selected_unavailable_owner_blocks_background_transport(self):
        self.native.get_all_stars = lambda: []
        self.bridge.memory_runtime = NS(
            read_one=lambda sql, args: (MEMORY_OWNER_PLUGIN_ID,)
        )
        envelope = await self.envelope()
        result = await self.native_model.generate(envelope)
        self.assertEqual(result.status, "not_started")
        self.assertEqual(result.diagnostics.error_code, "external_memory_unavailable")
        self.assertEqual(self.native.provider.client.state.calls, [])


@unittest.skipUnless(
    os.environ.get("MEMORY_COMPANION_CONTRACT_ROOT"),
    "external bridge fixture was not supplied",
)
class InstalledBridgeSummaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        root = Path(os.environ["MEMORY_COMPANION_CONTRACT_ROOT"]).resolve()
        package_name = "external_memory_contract_fixture"
        package = ModuleType(package_name)
        package.__path__ = [str(root)]
        previous = {
            name: value
            for name, value in sys.modules.items()
            if name == package_name or name.startswith(package_name + ".")
        }
        sys.modules[package_name] = package

        def cleanup():
            for name in tuple(sys.modules):
                if name == package_name or name.startswith(package_name + "."):
                    sys.modules.pop(name, None)
            sys.modules.update(previous)

        self.addCleanup(cleanup)
        self.bridge_type = importlib.import_module(
            package_name + ".core.bridge"
        ).MemoryCompanionBridge
        self.package_name = package_name

    async def test_actual_1105_compose_normalizes_private_group_and_applies_real_visibility_and_p5_gate(
        self,
    ):
        service_module = importlib.import_module(self.package_name + ".core.service")
        models = importlib.import_module(self.package_name + ".core.models")
        policy_type = importlib.import_module(
            self.package_name + ".core.visibility"
        ).VisibilityPolicy
        config_type = importlib.import_module(
            self.package_name + ".core.config"
        ).ConfigView
        service = object.__new__(service_module.MemoryCompanionService)
        service.config = config_type(
            {"private_companion_bridge": {"enable_p5_b1_bridge_gate": False}}
        )
        policy, calls = policy_type(), []

        async def compose(ctx, **arguments):
            calls.append((ctx, arguments))
            visible = models.MemoryRecord(
                id="same",
                scope=ctx.scope,
                session_id=ctx.session_id,
                platform=ctx.platform,
                group_id=ctx.group_id,
                visibility="private_pair" if ctx.scope == "private" else "group_public",
                metadata={"owner_bot_id": ctx.bot_id},
                subject=models.EntityRef(kind="user", id=ctx.user_id),
                content="same-window-memory",
            )
            other_bot = replace(
                visible,
                id="other-bot",
                metadata={"owner_bot_id": "other-native-bot"},
                content="private-other-bot",
            )
            other_window = replace(
                visible,
                id="other-window",
                session_id="other:FriendMessage:another-user",
                group_id="another-group",
                subject=models.EntityRef(kind="user", id="another-user"),
                content="other-window-memory",
            )
            return "\n".join(
                record.content
                for record in (visible, other_bot, other_window)
                if policy.is_visible(record, ctx)[0]
            )

        service._compose_memory_injection = compose
        owner = self.bridge_type(service)
        native = NS(
            get_all_stars=lambda: [
                NS(
                    name="MemoryCompanion",
                    activated=True,
                    module=NS(get_memory_companion_bridge=lambda: owner),
                )
            ]
        )
        for kind, target, bot_id in (
            (ScopeKind.PRIVATE, "user-openid", "unknown_selfid"),
            (ScopeKind.GROUP, "group-openid", "qq_official"),
        ):
            scope = NS(
                kind=kind,
                native_scope_id=target,
                platform="AstrBot",
                account_id="AstrBot",
            )
            proof = {
                "bot_id": bot_id,
                "persona_id": "original",
                "platform": "AstrBot",
                "account_id": "AstrBot",
                "user_id": target if kind is ScopeKind.PRIVATE else "",
                "group_id": target if kind is ScopeKind.GROUP else "",
            }
            runtime = NS(
                read_one=lambda sql, args: (
                    (MEMORY_OWNER_PLUGIN_ID,)
                    if args == (EXTERNAL_MEMORY_OWNER_KEY,)
                    else (json.dumps(proof),)
                )
            )
            bridge = NS(
                memory_runtime=runtime,
                verified_persona=AsyncMock(return_value={"persona_id": "original"}),
            )
            from companion_v2.conversation.foreground import PromptMaterial

            request = NS(
                event=NS(scope=scope),
                role_binding=object(),
                instruction="Recall this window",
                context=NS(
                    materials=(PromptMaterial("current", "current_source", "memory"),)
                ),
            )
            identity = native_memory_identity(
                native, scope, persona_id="original", runtime=runtime
            )
            result = await owner.compose_context(
                query="memory", session_context=identity, top_k=6, max_chars=4000
            )
            self.assertEqual(result, "same-window-memory")
            self.assertEqual(calls[-1][0].bot_id, bot_id)
            self.assertEqual(calls[-1][0].persona_id, "original")
            self.assertEqual(calls[-1][0].current_target_id, target)
            proof["account_id"] = "another-account"
            with self.assertRaises(TaskError):
                await with_external_memory(native, bridge, NS(original=request))
        service.config = config_type(
            {"private_companion_bridge": {"enable_p5_b1_bridge_gate": True}}
        )
        count = len(calls)
        self.assertEqual(
            await owner.compose_context(query="memory", session_context=identity), ""
        )
        self.assertEqual(len(calls), count)
        self.assertEqual(
            service._last_p5_gate_status["error_code"], "p5_attestation_required"
        )

    async def test_native_v2_request_does_not_trigger_v1_memory_suppression_markers(
        self,
    ):
        from astrbot_host.core_reply import (
            CORE_REPLY_CONTEXT_EXTRA,
            inject_core_reply_prompt,
        )

        service_type = importlib.import_module(
            self.package_name + ".core.service"
        ).MemoryCompanionService
        config_type = importlib.import_module(
            self.package_name + ".core.config"
        ).ConfigView
        detector = importlib.import_module(
            self.package_name + ".core.astrbot_compat"
        ).detect_private_companion_request
        service = object.__new__(service_type)
        service.config = config_type(
            {
                "private_companion_bridge": {
                    "dedupe_prompt_context": True,
                    "suppress_self_timeline_when_companion_seen": True,
                    "suppress_user_context_when_companion_seen": True,
                }
            }
        )
        prepared = {
            "stable_prompt_instruction": "Speak naturally from the current conversation.",
            "context": None,
            "voice_reply_policy": None,
        }
        extra = {CORE_REPLY_CONTEXT_EXTRA: {"prepared": prepared}}
        event = NS(get_extra=lambda key, default=None: extra.get(key, default))
        request = NS(
            system_prompt="Native persona",
            prompt="Recall our plans",
            contexts=[],
            extra_user_content_parts=[],
        )
        inject_core_reply_prompt(event, request)
        state = detector(request)
        present = service._companion_memory_context_present(state, set())
        slots = {"self_timeline": [object()], "user_profile": [object()]}
        kept, reasons = service._dedupe_slots_for_companion(slots, state, present)
        self.assertFalse(present)
        self.assertEqual(kept, slots)
        self.assertEqual(reasons, [])
        request.system_prompt += "<!-- private_companion_state_v1 -->"
        state = detector(request)
        present = service._companion_memory_context_present(state, set())
        kept, reasons = service._dedupe_slots_for_companion(slots, state, present)
        self.assertTrue(present)
        self.assertFalse(kept["self_timeline"])
        self.assertTrue(reasons)

    async def test_actual_1105_capability_authorizes_only_the_registered_native_identity(
        self,
    ):
        producer = type("RegisteredCompanion", (), {})()
        stars = [
            NS(
                activated=True,
                root_dir_name="astrbot_plugin_private_companion",
                name="PrivateCompanion",
                star_cls=producer,
                star_cls_type=type(producer),
            )
        ]
        native = NS(
            get_all_stars=lambda: stars,
            get_platform_inst=lambda _: NS(meta=lambda: NS(name="aiocqhttp")),
        )
        service = NS(
            context=native,
            read_user_memory_summary=AsyncMock(
                return_value={
                    "contract": "memory.user_memory_summary.v1",
                    "ok": True,
                    "state": "ready",
                    "counts": {"profile": 1, "total": 1},
                }
            ),
        )
        bridge = self.bridge_type(service)
        stars.append(
            NS(
                name="MemoryCompanion",
                activated=True,
                module=NS(get_memory_companion_bridge=lambda: bridge),
            )
        )
        scope = NS(
            kind=ScopeKind.PRIVATE,
            native_scope_id="200",
            platform="qq-main",
            account_id="100",
            bot_id="logical-bot",
        )
        native_bridge = NS(
            verified_persona=AsyncMock(return_value={"persona_id": "original"})
        )
        controller = NS(context=native, session=NS(bridge=native_bridge, runtime=None))
        result = await read_external_memory_summary(
            controller, NS(scope=scope, role_binding=object())
        )
        self.assertEqual(result["state"], "ready")
        service.read_user_memory_summary.assert_awaited_once_with(
            "200", session_id="qq-main:FriendMessage:200", limit=8
        )
        self.assertIsNone(bridge.register_private_companion(object()))
        bridge.deactivate()
        result = await read_external_memory_summary(
            controller, NS(scope=scope, role_binding=object())
        )
        self.assertEqual(result["state"], "not_owner")

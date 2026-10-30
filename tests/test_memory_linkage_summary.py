import tests.astrbot_data_root  # noqa: F401

import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from astrbot_host.memory_linkage import V2_PLUGIN_ID, read_external_memory_summary
from companion_v2.foundation.refs import ScopeKind


class SummaryBridge:
    def __init__(self, raw):
        self.raw = raw
        self.calls = []

    def register_private_companion(self, producer):
        self.calls.append(("register", producer))
        return "summary-capability"

    def create_user_memory_context(self, capability, **identity):
        self.calls.append(("context", capability, identity))
        return "summary-context"

    async def read_user_memory_summary(self, user_id, **arguments):
        self.calls.append(("read", user_id, arguments))
        return self.raw


class ExternalMemorySummaryTests(unittest.IsolatedAsyncioTestCase):
    def controller(self, bridge):
        producer = object()
        metadata = SimpleNamespace(activated=True, root_dir_name=V2_PLUGIN_ID, name=V2_PLUGIN_ID,
                                   star_cls=producer)
        context = SimpleNamespace(get_all_stars=lambda: [metadata],
            get_platform_inst=lambda _: SimpleNamespace(meta=lambda: SimpleNamespace(name="aiocqhttp")))
        bridge_port = SimpleNamespace(verified_persona=AsyncMock(return_value={"persona_id": "persona-1"}))
        controller = SimpleNamespace(context=context, session=SimpleNamespace(bridge=bridge_port, runtime=None))
        owner = SimpleNamespace(
            ownership=SimpleNamespace(delegated=True, provider="astrbot_plugin_remember_you",
                                      display_name="记忆插件"),
            handle=bridge,
        )
        return controller, owner, producer

    async def test_reads_only_exact_private_summary_counts_and_discards_content(self):
        raw = {
            "contract": "memory.user_memory_summary.v1", "ok": True, "state": "ready",
            "counts": {"profile": 2, "preference": 3, "relationship": 1,
                       "private_conversation": 4, "other": -5, "total": 10},
            "summaries": [{"summary": "private content must not cross this adapter"}],
        }
        bridge = SummaryBridge(raw)
        controller, owner, producer = self.controller(bridge)
        scope = SimpleNamespace(kind=ScopeKind.PRIVATE, native_scope_id="user-1", platform="qq",
                                bot_id="bot-1", account_id="100")
        context = SimpleNamespace(scope=scope, role_binding=object())

        with patch("astrbot_host.memory_linkage.read_memory_owner", return_value=owner):
            result = await read_external_memory_summary(controller, context)

        self.assertEqual(result["state"], "ready")
        self.assertEqual(result["counts"], {"profile": 2, "preference": 3, "relationship": 1,
                                             "private_conversation": 4, "other": 0, "total": 10})
        self.assertNotIn("summaries", result)
        self.assertNotIn("private content", str(result))
        self.assertEqual(bridge.calls[0], ("register", producer))
        read_call = bridge.calls[-1]
        self.assertEqual(read_call[1], "user-1")
        self.assertEqual(read_call[2]["session_id"], "qq:FriendMessage:user-1")
        self.assertEqual(read_call[2]["limit"], 8)

    async def test_does_not_read_when_external_plugin_is_not_the_memory_owner(self):
        bridge = SummaryBridge({})
        controller, owner, _ = self.controller(bridge)
        owner.ownership.delegated = False
        context = SimpleNamespace(scope=SimpleNamespace(kind=ScopeKind.PRIVATE, native_scope_id="user-1",
                                                        platform="qq", bot_id="bot-1"))

        with patch("astrbot_host.memory_linkage.read_memory_owner", return_value=owner):
            result = await read_external_memory_summary(controller, context)

        self.assertEqual(result["state"], "not_owner")
        self.assertEqual(bridge.calls, [])

    async def test_target_view_uses_host_bridge_and_exact_qq_identity_proof(self):
        from astrbot_host.multi_api import TargetControllerView

        bridge = SummaryBridge(
            {
                "contract": "memory.user_memory_summary.v1",
                "ok": True,
                "state": "ready",
                "counts": {"total": 1},
            }
        )
        controller, owner, _ = self.controller(bridge)
        scope = SimpleNamespace(
            kind=ScopeKind.PRIVATE,
            native_scope_id="user-1",
            platform="official",
            account_id="AstrBot",
        )
        context = SimpleNamespace(scope=scope, role_binding=object())
        proof = {
            "persona_id": "persona-1",
            "platform": "official",
            "account_id": "AstrBot",
            "user_id": "user-1",
            "group_id": "",
            "bot_id": "unknown_selfid",
        }
        controller.runtime = SimpleNamespace(
            read_one=Mock(return_value=(json.dumps(proof),))
        )
        controller.session.runtime = controller.runtime
        controller.session.application = object()
        controller.authority = SimpleNamespace(context=lambda _target, _role: context)
        view = TargetControllerView(controller, "target-1", "role-1")
        self.assertFalse(hasattr(view.session, "bridge"))

        with patch("astrbot_host.memory_linkage.read_memory_owner", return_value=owner):
            result = await read_external_memory_summary(view, context)

        self.assertEqual(result["state"], "ready")
        controller.runtime.read_one.assert_called_once()
        self.assertEqual(
            controller.runtime.read_one.call_args.args[1],
            ("external_memory_identity:official:FriendMessage:user-1",),
        )
        self.assertEqual(bridge.calls[1][2]["bot_id"], "unknown_selfid")
        self.assertEqual(
            bridge.calls[-1][2]["session_id"], "official:FriendMessage:user-1"
        )

    async def test_rejects_group_scope_before_calling_external_bridge(self):
        bridge = SummaryBridge({})
        controller, owner, _ = self.controller(bridge)
        context = SimpleNamespace(scope=SimpleNamespace(kind=ScopeKind.GROUP, native_scope_id="group-1",
                                                        platform="qq", bot_id="bot-1"))

        with patch("astrbot_host.memory_linkage.read_memory_owner", return_value=owner):
            result = await read_external_memory_summary(controller, context)

        self.assertEqual(result["state"], "unsupported_scope")
        self.assertEqual(bridge.calls, [])


if __name__ == "__main__":
    unittest.main()

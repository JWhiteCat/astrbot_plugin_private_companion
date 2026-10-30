"""V1 admission boundaries, durable native receipts and source-AST comparisons."""

import tests.astrbot_data_root  # noqa: F401

import ast
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import itertools
import json
import os
from pathlib import Path
import random
import re
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
from typing import Any
import unittest
from unittest.mock import Mock, patch

from companion_v2.conversation.group_admission import GroupReplyAdmission
from companion_v2.conversation.group_questions import legacy_cold_group_score
from companion_v2.conversation.legacy_group_runtime import (
    KIND,
    LegacyGroupRuntime,
    atmosphere,
    build_import,
    cycle_profile,
    import_reviewed,
    interjection_probability,
    owner_key,
)
from companion_v2.foundation.refs import ScopeKind, ScopeRef, SubjectKind, SubjectRef
from companion_v2.storage.runtime import StorageRuntime


AT = datetime(2026, 10, 29, 12, tzinfo=timezone.utc)
TODAY = AT.date().isoformat()


def source_method(root, filename, name, namespace):
    """Compile one frozen method without importing or executing its plugin.

    Args:
        root: Explicit read-only source directory.
        filename: Source module filename.
        name: Exact method name to extract.
        namespace: Explicit test substitutes for external runtime inputs.

    Returns:
        Executable original method AST.
    """
    tree = ast.parse((root / filename).read_text(encoding="utf-8-sig"))
    node = next(
        item
        for item in ast.walk(tree)
        if isinstance(item, ast.FunctionDef) and item.name == name
    )
    node.decorator_list = []
    scope = {
        "Any": Any,
        "re": re,
        "datetime": datetime,
        "random": random,
        "_today_key": lambda: TODAY,
        "_now_ts": lambda: AT.timestamp(),
        "_single_line": lambda value, size: " ".join(str(value or "").split())[:size],
        "_safe_int": lambda value, default=0, *bounds: (
            max(bounds[0], min(bounds[1], int(value)))
            if len(bounds) == 2
            else max(bounds[0], int(value))
            if bounds
            else int(value)
        ),
        "_safe_float": lambda value, default=0, *bounds: float(value or default),
        **namespace,
    }
    exec(
        compile(
            ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])),
            str(root / filename),
            "exec",
        ),
        scope,
    )
    return scope[name]


class LegacyGroupRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.runtime = StorageRuntime.open(
            data_root=self.root / "runtime", installation_id="test"
        )
        self.addCleanup(self.runtime.close)
        self.scope = ScopeRef(
            "test",
            "official",
            ScopeKind.GROUP,
            "GROUP",
            "bot",
            "official",
            "GROUP",
            "GROUP",
        )
        self.context = NS(
            scope=self.scope,
            key="context",
            role_binding=NS(
                role=SubjectRef("test", SubjectKind.BOT, "role"), revision=0
            ),
            actor=NS(principal=NS(native_account_id="MEMBER")),
        )
        self.source = {
            "groups": {
                "GROUP": {
                    "last_seen": AT.timestamp() - 3600,
                    "interject_day": TODAY,
                    "interject_today": 27,
                    "last_interject_at": AT.timestamp() - 3600,
                    "recent_messages": [],
                }
            },
            "daily_state": {
                "date": TODAY,
                "energy": 70,
                "sleep_runtime": {"phase": "awake"},
            },
        }
        self.source_root = self.root / "source"
        self.source_root.mkdir()
        (self.source_root / "companions.json").write_text(
            json.dumps(self.source), encoding="utf-8"
        )
        self.plan = build_import(
            self.source_root, source_persona_id="", source_group_id="GROUP"
        )
        import_reviewed(self.runtime, self.plan, self.context)
        self.service = LegacyGroupRuntime(self.runtime)
        self.messages = NS(
            runtime=self.runtime,
            timezone_name="UTC",
            tasks=NS(now=lambda: AT),
            group_context=NS(authorize=Mock()),
        )

    def test_import_is_idempotent_and_owner_ignores_member_session(self):
        group, _ = self.service.read(self.context)
        self.assertEqual(group["interject_today"], 27)
        other = NS(**vars(self.context))
        other.scope = ScopeRef(
            "test",
            "official",
            ScopeKind.GROUP,
            "GROUP",
            "bot",
            "official",
            "GROUP",
            "OTHER_GROUP",
        )
        self.assertEqual(owner_key(other), owner_key(self.context))
        self.assertEqual(self.service.read(other)[0]["interject_today"], 27)
        self.assertEqual(
            import_reviewed(self.runtime, self.plan, self.context)["state"], "unchanged"
        )
        other.role_binding = NS(role=SubjectRef("test", SubjectKind.BOT, "different"))
        self.assertIsNone(self.service.read(other))
        self.assertFalse(self.plan["state_evolution_equivalent"])

    def test_observation_preserves_previous_seen_for_current_event_and_restart(self):
        admission = GroupReplyAdmission(self.messages)
        admission.observe(
            self.context,
            text="有人吗",
            speaker_id="MEMBER",
            received_at=AT,
            event_id="one",
        )
        self.assertTrue(
            self.service.cold_ready(
                self.context, at=AT, event_id="one", idle_minutes=25
            )
        )
        admission.observe(
            self.context,
            text="有人吗",
            speaker_id="MEMBER",
            received_at=AT,
            event_id="one",
        )
        self.assertTrue(
            self.service.cold_ready(
                self.context, at=AT, event_id="one", idle_minutes=25
            )
        )
        later = AT + timedelta(seconds=1)
        restarted = GroupReplyAdmission(self.messages)
        restarted.observe(
            self.context,
            text="还有人吗",
            speaker_id="MEMBER",
            received_at=later,
            event_id="two",
        )
        self.assertFalse(
            self.service.cold_ready(
                self.context, at=later, event_id="two", idle_minutes=25
            )
        )
        settings = {
            "group_wakeup_legacy_targeting": True,
            "group_wakeup_question_enabled": False,
            "group_wakeup_cold_group_enabled": True,
            "group_wakeup_cold_group_threshold": 80,
        }
        self.assertEqual(
            restarted.candidate(
                self.context,
                text="有人吗",
                instruction="",
                at=later,
                event_id="two",
                settings=settings,
                participation={
                    "enable_group_repeat_follow": False,
                    "enable_group_interjection": False,
                },
                continuation={"limit_reached": False, "candidate_reason": ""},
            ),
            "",
        )

    def test_same_timestamp_replayed_observation_is_not_folded_twice(self):
        admission = GroupReplyAdmission(self.messages)
        for event in ("first", "second", "first"):
            admission.observe(
                self.context,
                text="笑死",
                speaker_id="MEMBER",
                received_at=AT,
                event_id=event,
            )
        group, _ = self.service.read(self.context)
        self.assertEqual(
            [item["event_id"] for item in group["recent"]], ["first", "second"]
        )
        self.assertEqual(group["observed_event"], "second")

    def test_history_replacement_preserves_progressed_admission_counter(self):
        with self.runtime.transaction() as connection:
            self.service.record_returned(
                connection,
                self.context,
                {"reason": "interjection", "send_returns": 0},
                at=AT,
                timezone_name="UTC",
            )
        self.source["groups"]["GROUP"]["recent_messages"] = [
            {
                "ts": AT.timestamp() - 60,
                "sender_id": "MEMBER",
                "text": "synthetic history",
            }
        ]
        (self.source_root / "companions.json").write_text(
            json.dumps(self.source), encoding="utf-8"
        )
        changed = build_import(
            self.source_root, source_persona_id="", source_group_id="GROUP"
        )
        result = import_reviewed(self.runtime, changed, self.context)
        self.assertEqual(result["state"], "preserved_runtime")
        self.assertEqual(self.service.read(self.context)[0]["interject_today"], 28)
        self.assertEqual(result["runtime_source_fingerprint"], self.plan["fingerprint"])

    def test_receipt_counter_survives_history_eviction_and_counts_only_once(self):
        admission = GroupReplyAdmission(self.messages)
        for index in range(150):
            event = str(index)
            reason = "interjection" if index < 2 else "mention"
            admission.record_core_reply(
                self.context,
                event,
                instruction="persona",
                reason=reason,
                mentioned=reason == "mention",
                at=AT,
            )
            admission.mark_core_reply(self.context, event, stage="send_started", at=AT)
            admission.mark_core_reply(self.context, event, stage="send_returned", at=AT)
            admission.mark_core_reply(self.context, event, stage="send_returned", at=AT)
        self.assertEqual(len(admission._core_turns(self.context)), 128)
        group, _ = LegacyGroupRuntime(self.runtime).read(self.context)
        self.assertEqual(group["interject_today"], 29)
        self.assertEqual(group["last_interject_at"], AT.timestamp())
        admission.record_core_reply(
            self.context,
            "unknown",
            instruction="persona",
            reason="interjection",
            mentioned=False,
            at=AT,
        )
        admission.mark_core_reply(self.context, "unknown", stage="send_unknown", at=AT)
        self.assertEqual(self.service.read(self.context)[0]["interject_today"], 29)
        admission.record_core_reply(
            self.context,
            "repeat",
            instruction="persona",
            reason="repeat",
            mentioned=False,
            at=AT,
        )
        admission.mark_core_reply(self.context, "repeat", stage="send_returned", at=AT)
        self.assertEqual(self.service.read(self.context)[0]["interject_today"], 30)
        self.assertEqual(self.service.read(self.context)[0]["native_repeat_today"], 1)

    def test_hard_quota_cooldown_probability_and_day_rollover(self):
        values = {
            "group_interject_max_daily": 30,
            "group_interject_min_interval_minutes": 10,
        }
        with patch(
            "companion_v2.conversation.legacy_group_runtime.random.random",
            return_value=0.0,
        ) as draw:
            self.assertTrue(
                self.service.interject(
                    self.context,
                    "笑死",
                    at=AT,
                    event_id="one",
                    values=values,
                    timezone_name="UTC",
                )
            )
            self.assertTrue(
                self.service.interject(
                    self.context,
                    "笑死",
                    at=AT,
                    event_id="one",
                    values=values,
                    timezone_name="UTC",
                )
            )
            self.assertEqual(draw.call_count, 1)
        group, role = self.service.read(self.context)
        group["interject_today"] = 30
        with self.runtime.transaction() as connection:
            self.service.store.write(connection, owner_key(self.context), KIND, group)
        with patch(
            "companion_v2.conversation.legacy_group_runtime.random.random",
            return_value=0.0,
        ) as draw:
            self.assertFalse(
                self.service.interject(
                    self.context,
                    "笑死",
                    at=AT,
                    event_id="closed",
                    values=values,
                    timezone_name="UTC",
                )
            )
            draw.assert_not_called()
            self.assertTrue(
                self.service.interject(
                    self.context,
                    "笑死",
                    at=AT + timedelta(days=1),
                    event_id="next-day",
                    values=values,
                    timezone_name="UTC",
                )
            )
        group["interject_today"] = 0
        group["last_interject_at"] = AT.timestamp() - 599
        self.assertEqual(
            interjection_probability(
                group,
                "笑死",
                at=AT.timestamp(),
                today=TODAY,
                max_daily=30,
                interval_minutes=10,
                daily_state=role["daily_state"],
                cycle_enabled=True,
            ),
            0,
        )

    def test_candidate_consumes_hard_gate_without_blocking_explicit_mentions(self):
        admission = GroupReplyAdmission(self.messages)
        options = {
            "context": self.context,
            "instruction": "",
            "at": AT,
            "event_id": "candidate",
            "settings": {
                "group_wakeup_legacy_targeting": True,
                "group_wakeup_question_enabled": False,
            },
            "participation": {
                "enable_group_repeat_follow": False,
                "enable_group_interjection": True,
                "group_interject_max_daily": 27,
                "group_interject_min_interval_minutes": 10,
            },
            "continuation": {"limit_reached": False, "candidate_reason": ""},
        }
        with patch(
            "companion_v2.conversation.legacy_group_runtime.random.random",
            return_value=0.0,
        ) as draw:
            self.assertEqual(admission.candidate(text="笑死", **options), "")
            self.assertEqual(
                admission.candidate(text="笑死", mentioned=True, **options), "mention"
            )
            draw.assert_not_called()
            options["participation"]["group_interject_max_daily"] = 30
            options["event_id"] = "open-candidate"
            self.assertEqual(
                admission.candidate(text="笑死", **options), "interjection"
            )


@unittest.skipUnless(
    os.environ.get("LEGACY_COMPANION_SOURCE"),
    "source AST requires an explicit frozen plugin directory",
)
class LegacyGroupSourceASTTests(LegacyGroupRuntimeTests):
    def setUp(self):
        super().setUp()
        self.old = Path(os.environ["LEGACY_COMPANION_SOURCE"])
        self.values = {
            "enable_group_interjection": True,
            "group_interject_max_daily": 30,
            "group_interject_min_interval_minutes": 10,
            "enable_cycle_state": True,
            "enable_group_wakeup_cold_group": True,
            "group_wakeup_cold_group_idle_minutes": 25,
        }

        def setting(host, key, default):
            return self.values.get(key, default)

        self.host = NS(
            data={"daily_state": self.plan["role"]["daily_state"]},
            _filtered_group_recent_messages=lambda group: group.get(
                "recent_messages", []
            ),
            _group_wakeup_reason_label=lambda *args: "test",
        )
        namespace = {
            "_persona_value": setting,
            "_proactive_setting_value": setting,
            "_group_link_message_context": lambda text: (text, False),
        }
        self.cycle = source_method(
            self.old, "proactive.py", "_cycle_proactive_frequency_profile", namespace
        )
        self.host._cycle_proactive_frequency_profile = lambda: self.cycle(self.host)
        self.host._cycle_group_interject_probability = lambda value: (
            value * self.cycle(self.host)["group_probability_multiplier"]
        )
        self.host._effective_group_interject_min_interval_minutes = lambda: max(
            1, min(1440, round(10 * self.cycle(self.host)["group_interval_multiplier"]))
        )
        self.allowed = source_method(
            self.old, "group_observation.py", "_group_interjection_allowed", namespace
        )
        self.cold = source_method(
            self.old, "group_wakeup.py", "_group_wakeup_cold_group_signal", namespace
        )
        self.old_atmosphere = source_method(
            self.old, "group_observation.py", "_update_group_atmosphere", namespace
        )

    def test_source_ast_cold_state_idle_and_lexical_boundaries(self):
        cases = itertools.product(
            (
                "awake",
                "natural_wake",
                "falling_asleep",
                "light_sleep",
                "sleeping_again",
                "woken",
            ),
            (0, 35, 36, 70, 100),
            (0, 1499, 1500, 3600),
            ("有人吗", "大家早上好", "请求失败怎么办", "救命笑死了"),
        )
        for phase, energy, idle, text in cases:
            state = {"energy": energy, "sleep_runtime": {"phase": phase}}
            self.host.data["daily_state"] = state
            last = AT.timestamp() - idle if idle else 0
            old = self.cold(self.host, {"last_seen": last}, text, AT.timestamp())
            group, role = self.service.read(self.context)
            group["last_seen"] = last
            role["daily_state"] = state
            with self.runtime.transaction() as connection:
                self.service.store.write(
                    connection, owner_key(self.context), KIND, group
                )
                connection.execute(
                    "UPDATE runtime_metadata SET value = ? WHERE key = ?",
                    (json.dumps(role), self.service.role_key(self.context)),
                )
            score = (
                legacy_cold_group_score(text)
                if self.service.cold_ready(
                    self.context, at=AT, event_id="new", idle_minutes=25
                )
                else 0
            )
            self.assertEqual(score, old.get("score", 0), (phase, energy, idle, text))

    def test_source_ast_cycle_probability_and_hard_gate_matrix(self):
        states = (
            {},
            {"date": TODAY, "body_cycle": "PMS"},
            {"date": TODAY, "body_cycle": "月经期"},
            {"date": TODAY, "body_cycle": "生理期后恢复"},
            {"date": "2000-01-01", "body_cycle": "月经期"},
        )
        for state, mood, pace, text, used, elapsed, roll in itertools.product(
            states,
            ("玩笑", "求助", "平稳"),
            ("安静", "热闹"),
            ("笑死", "有没有人", "普通内容", "别插话", "@某人 笑死"),
            (29, 30),
            (599, 900),
            (0.0, 0.01, 0.05, 0.1),
        ):
            self.host.data["daily_state"] = state
            group = {
                "interject_day": TODAY,
                "interject_today": used,
                "last_interject_at": AT.timestamp() - elapsed,
                "atmosphere": {"mood": mood, "pace": pace},
            }
            probability = interjection_probability(
                group,
                text,
                at=AT.timestamp(),
                today=TODAY,
                max_daily=30,
                interval_minutes=10,
                daily_state=state,
                cycle_enabled=True,
            )
            with patch.object(random, "random", return_value=roll):
                expected = self.allowed(self.host, deepcopy(group), text)[0]
            self.assertEqual(
                probability > roll,
                expected,
                (state, mood, pace, text, used, elapsed, roll),
            )
        for state in states:
            self.host.data["daily_state"] = state
            old = self.cycle(self.host)
            self.assertEqual(
                cycle_profile(state, enabled=True, today=TODAY),
                (old["group_interval_multiplier"], old["group_probability_multiplier"]),
            )

    def test_source_ast_atmosphere_uses_identical_window_and_precedence(self):
        for count, speakers, text, age in itertools.product(
            (1, 6, 18),
            (1, 2, 6),
            ("hello", "哈哈", "烦累", "笑死但是怎么报错了", "闭嘴"),
            (719, 720, 721),
        ):
            recent = [
                {
                    "ts": AT.timestamp() - age,
                    "sender_id": str(index % speakers),
                    "text": text,
                }
                for index in range(count)
            ]
            group = {"recent_messages": recent}
            self.old_atmosphere(self.host, group)
            self.assertEqual(
                atmosphere(recent, at=AT.timestamp()),
                {key: group["atmosphere"][key] for key in ("mood", "pace")},
            )


if __name__ == "__main__":
    unittest.main()

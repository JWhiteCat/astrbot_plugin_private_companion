"""Equivalent configuration boundaries for a frozen QQ Official V1 installation."""

import tests.astrbot_data_root  # noqa: F401

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

from astrbot_host.group_settings import GroupTarget
from astrbot_host.multi_settings import MultiHostSettings
from companion_v2.adapters.astrbot.native import HostAccount
from companion_v2.configuration.registry import (
    FieldDefinition,
    FieldRegistry,
    ValueKind,
)
from companion_v2.conversation.group_admission import GroupReplyAdmission
from companion_v2.conversation.group_questions import legacy_cold_group_score
from companion_v2.conversation.participation import (
    GroupParticipation,
    GroupParticipationConfigurationOwner,
    configuration_fields as participation_fields,
    group_participation_settings,
)
from companion_v2.conversation.segmentation import SegmentationPolicy
from companion_v2.conversation.reply_settings import configuration_fields
from companion_v2.legacy.configuration_migration import (
    ConfigurationDifference,
    ConfigurationMigration,
    convert_account_configuration,
    convert_configuration,
    convert_plugin_configuration,
)
from companion_v2.legacy.model_configuration import expand_quick_model_configuration
from companion_v2.legacy.proactive_configuration import private_contact_configuration
from companion_v2.legacy.strict_configuration_audit import strict_configuration_audit
from companion_v2.role_work.state.configuration import (
    configuration_fields as state_fields,
)
from tests.contact_schedule_support import ContactScheduleFixture
from tests.test_proactive_news_events import NewsPort


class Legacy656ConfigurationTests(unittest.TestCase):
    def test_grouped_values_override_flat_aliases_and_followup_stays_disabled(self):
        config = {
            "enable_group_conversation_followup": True,
            "private_followup_config": {
                "enable_group_conversation_followup": False,
                "group_conversation_followup_seconds": 500,
                "group_conversation_followup_max_turns": 1,
            },
            "group_wakeup_config": {
                "enable_group_wakeup_enhancement": True,
                "enable_group_wakeup_question": True,
                "enable_group_wakeup_cold_group": True,
                "group_wakeup_interest_probability": 99,
            },
        }
        registry = FieldRegistry(
            version="test", fields=configuration_fields(SegmentationPolicy(), False, 0)
        )
        result = convert_configuration(config, registry=registry, window_kind="group")
        prefix = "conversation.presentation."
        self.assertIs(
            result.changes[prefix + "group_conversation_followup_enabled"], False
        )
        self.assertEqual(
            result.changes[prefix + "group_conversation_followup_seconds"], 500
        )
        self.assertEqual(
            result.changes[prefix + "group_conversation_followup_max_turns"], 1
        )
        self.assertEqual(
            result.changes[prefix + "group_wakeup_interest_probability"], 0.99
        )
        self.assertIs(result.changes[prefix + "group_wakeup_legacy_targeting"], True)
        self.assertTrue(
            any(row.kind == "group_precedence" for row in result.differences)
        )

    def test_empty_blacklist_produces_dynamic_official_group_route(self):
        user = "A" * 32
        source = {
            "basic_config": {
                "target_platform": "qq_official",
                "target_user_ids": [user],
            },
            "group_observation_config": {
                "enable_group_companion": True,
                "group_access_mode": "blacklist",
                "group_blacklist_ids": [],
                "group_whitelist_ids": ["B" * 32],
            },
        }
        accounts = [
            {
                "platform_id": "official",
                "account_id": "official",
                "bot_id": "bot",
                "default_persona_id": "character",
                "persona_roles": {"character": "role"},
                "native_windows": [
                    {
                        "scope_kind": "private",
                        "native_scope_id": user,
                        "persona_id": "character",
                        "unified_msg_origin": "official:FriendMessage:" + user,
                    }
                ],
            }
        ]
        migration = convert_account_configuration(source, accounts=accounts)
        groups = tuple(
            GroupTarget(**row) for row in migration.changes["multi_host.group_targets"]
        )
        self.assertEqual([row.group_id for row in groups], ["*"])
        self.assertEqual(
            migration.changes["multi_host.targets"][0]["participant_id"], user
        )
        settings = NS(
            group_targets=groups, accounts=(HostAccount("official", "official", "bot"),)
        )
        for group in ("B" * 32, "NEW_GROUP_OPENID"):
            self.assertEqual(
                MultiHostSettings.group_target(
                    settings, "official", "official", group
                ).group_id,
                group,
            )
        self.assertIsNone(
            MultiHostSettings.group_target(
                settings, "other", "official", "NEW_GROUP_OPENID"
            )
        )

    def test_daily_interjection_limit_preserves_legacy_thirty(self):
        registry = FieldRegistry(version="test", fields=participation_fields())
        result = convert_configuration(
            {
                "group_interject_config": {
                    "enable_group_interjection": True,
                    "group_interject_max_daily": 30,
                }
            },
            registry=registry,
            window_kind="group",
        )
        self.assertEqual(
            result.changes[
                "conversation.group_participation.group_interject_max_daily"
            ],
            30,
        )

    def test_independent_state_and_group_windows_do_not_conflict(self):
        registry = FieldRegistry(
            version="test",
            fields=(
                *configuration_fields(SegmentationPolicy(), False, 0),
                *state_fields(),
            ),
        )
        config = {
            "humanized_state_config": {"enable_humanized_states": True},
            "schedule_detail_config": {"enable_detail_enhancement": False},
            "private_followup_config": {"group_air_guard_window_seconds": 180},
            "group_high_intensity_config": {
                "enable_group_high_intensity_mode": True,
                "group_high_intensity_wakeup_window_seconds": 60,
            },
        }
        result = convert_configuration(config, registry=registry, window_kind="group")
        self.assertIs(result.changes["role.state.enabled"], True)
        prefix = "conversation.presentation."
        self.assertEqual(result.changes[prefix + "group_air_guard_window_seconds"], 180)
        self.assertEqual(
            result.changes[prefix + "group_high_intensity_window_seconds"], 60
        )
        self.assertNotIn("conflict", {row.kind for row in result.differences})
        participation = GroupParticipation.__new__(GroupParticipation)
        now = datetime.now(timezone.utc)
        participation.admission = NS(
            native_participation_history=Mock(
                return_value=(
                    [],
                    [
                        {"returned_at": (now - timedelta(seconds=seconds)).isoformat()}
                        for seconds in (120, 70, 20)
                    ],
                )
            )
        )
        settings = {
            key.removeprefix(prefix): value
            for key, value in result.changes.items()
            if key.startswith(prefix)
        }
        self.assertFalse(
            participation.high_intensity_state(None, "", at=now, settings=settings)[
                "active"
            ]
        )
        settings["group_high_intensity_window_seconds"] = 0
        self.assertTrue(
            participation.high_intensity_state(None, "", at=now, settings=settings)[
                "active"
            ]
        )

    def test_nonempty_blacklist_remains_a_blocker(self):
        source = {
            "group_observation_config": {
                "enable_group_companion": True,
                "group_access_mode": "blacklist",
                "group_blacklist_ids": ["300"],
            }
        }
        result = convert_account_configuration(source, accounts=[])
        self.assertNotIn("multi_host.group_targets", result.changes)
        self.assertIn("unsupported_mode", {row.kind for row in result.differences})

    def test_quick_routing_uses_runtime_assignment_and_preserves_source(self):
        config = {
            "model_assignment_config": {
                "provider_config_mode": "quick",
                "FAST_RESPONSE_PROVIDER_ID": "fast",
                "COMPLEX_REASONING_PROVIDER_ID": "complex",
                "DAILY_PLAN_PROVIDER_ID": "inactive-precision",
                "enable_llm_streaming": True,
            }
        }
        frozen = json.dumps(config, sort_keys=True)
        fields = tuple(
            FieldDefinition(name + ".provider", name, name, ValueKind.TEXT, "default")
            for name in ("model.calendar.plan", "model.proactive.judge")
        )
        result = convert_configuration(
            config, registry=FieldRegistry(version="test", fields=fields)
        )
        self.assertEqual(result.changes["model.calendar.plan.provider"], "complex")
        self.assertEqual(result.changes["model.proactive.judge.provider"], "complex")
        self.assertEqual(
            convert_plugin_configuration(config).changes["model.provider_id"], "complex"
        )
        expanded = expand_quick_model_configuration(config)
        self.assertEqual(expanded["GROUP_INTERJECT_PROVIDER_ID"], "fast")
        self.assertEqual(expanded["DREAM_DIARY_PROVIDER_ID"], "complex")
        self.assertEqual(json.dumps(config, sort_keys=True), frozen)
        config["model_assignment_config"]["provider_config_mode"] = "precision"
        self.assertIs(expand_quick_model_configuration(config), config)

    def test_official_personal_quota_requires_exact_persona_evidence(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "companions.json"
            payload = {
                "users": {
                    "person": {
                        "identity_subject_id": "OPENID",
                        "identity_adapter_instance_id": "official",
                        "identity_bot_id": "official",
                        "identity_platform_kind": "qq_official",
                        "proactive_private_enabled": True,
                        "unified_profile_capabilities": {
                            "proactive_private_enabled": True
                        },
                        "proactive_daily_limit": 30,
                        "proactive_idle_minutes": -1,
                        "proactive_min_interval_minutes": -1,
                    }
                },
                "groups": {},
                "version": 1,
            }
            path.write_text(json.dumps(payload), encoding="utf-8")
            options = dict(
                persona_id="character",
                platform_id="official",
                account_id="official",
                participant_id="OPENID",
            )
            self.assertFalse(
                private_contact_configuration(directory, {}, **options).changes
            )
            result = private_contact_configuration(
                directory, {}, verified_primary_persona_id="character", **options
            )
            self.assertEqual(
                result.changes,
                {"enabled": True, "automatic_enabled": True, "max_contacts_24h": 30},
            )
            self.assertNotIn("idle_seconds", result.changes)
            self.assertNotIn("interval_seconds", result.changes)
            options["account_id"] = "other"
            self.assertFalse(
                private_contact_configuration(
                    directory, {}, verified_primary_persona_id="character", **options
                ).changes
            )

    def test_strict_audit_does_not_approve_defaults_or_mapping_without_evidence(self):
        schema = {
            "voice_action_config": {
                "items": {
                    "enable_voice_action": {"type": "bool", "default": False},
                    "enable_tts_enhancement": {"type": "bool", "default": False},
                    "tts_trigger_probability": {"type": "int", "default": 25},
                }
            },
            "news_config": {
                "items": {"news_share_probability": {"type": "float", "default": 1.0}}
            },
        }
        config = {
            "voice_action_config": {
                "enable_voice_action": False,
                "enable_tts_enhancement": False,
                "tts_trigger_probability": 25,
            },
            "news_config": {"news_share_probability": 1.0},
            "undocumented": True,
        }
        result = strict_configuration_audit(config, schema)
        self.assertEqual(result["field_count"], 4)
        self.assertFalse(result["ready"])
        fields = {row["source"]: row for row in result["fields"]}
        self.assertEqual(
            fields["voice_action_config.tts_trigger_probability"]["status"], "inactive"
        )
        self.assertEqual(
            fields["news_config.news_share_probability"]["status"], "blocked"
        )
        self.assertEqual(
            result["extra_fields"], [{"source": "undocumented", "status": "blocked"}]
        )
        self.assertTrue(
            strict_configuration_audit(
                config,
                schema,
                verified_sources=(
                    "voice_action_config.enable_voice_action",
                    "voice_action_config.enable_tts_enhancement",
                    "news_config.news_share_probability",
                    "undocumented",
                ),
            )["ready"]
        )

    def test_legacy_explicit_recipient_guards_preserve_strong_calls(self):
        admission = GroupReplyAdmission.__new__(GroupReplyAdmission)
        admission.messages = NS(
            group_context=NS(authorize=Mock()), tasks=NS(now=lambda: None)
        )
        admission._quoted_reply = Mock(return_value=False)
        context = NS(actor=NS(principal=NS(native_account_id="OPENID")))
        settings = {
            "group_wakeup_legacy_targeting": True,
            "group_wakeup_direct_words": '["Companion"]',
        }
        options = dict(
            context=context,
            instruction="",
            at=datetime.now(timezone.utc),
            settings=settings,
            participation={},
        )
        for recipient in (
            {"mentions_others": True},
            {"mentions_all": True},
            {"reply_ids": ("other-message",)},
        ):
            self.assertEqual(
                admission.candidate(text="why is this broken?", **recipient, **options),
                "",
            )
            self.assertEqual(
                admission.candidate(text="Companion, help", **recipient, **options),
                "direct_word",
            )
        self.assertEqual(
            admission.candidate(
                text="help", mentioned=True, mentions_others=True, **options
            ),
            "mention",
        )
        self.assertEqual(
            admission.candidate(
                text="help", native_wakeup=True, mentions_others=True, **options
            ),
            "native_wakeup",
        )

    def test_legacy_cold_group_text_and_threshold_are_consumed(self):
        for text, expected in (
            ("有人吗", 86),
            ("大家早上好", 72),
            ("请求失败怎么办", 78),
            ("今天这件事真不错", 0),
            ("怎么会这样？", 0),
            ("救命笑死我了", 0),
            ("急急如律令", 0),
            ("/help 有没有人", 0),
            ("看看 https://example.test", 0),
            ("[图片]有人吗", 0),
            ("早", 0),
            ("长" * 121, 0),
        ):
            self.assertEqual(legacy_cold_group_score(text), expected, text)
        admission = GroupReplyAdmission.__new__(GroupReplyAdmission)
        now = datetime.now(timezone.utc)
        admission.messages = NS(
            group_context=NS(authorize=Mock()), tasks=NS(now=lambda: now)
        )
        admission._quoted_reply = Mock(return_value=False)
        admission._key = Mock(return_value="group")
        admission._behavior_candidate = Mock(return_value=False)
        admission.runtime = Mock()
        admission.observations = {
            "group": [{"received_at": (now - timedelta(hours=1)).isoformat()}]
        }
        options = dict(
            context=NS(actor=NS(principal=NS(native_account_id="OPENID"))),
            instruction="",
            at=now,
            continuation={"limit_reached": False, "candidate_reason": ""},
            participation={
                "enable_group_repeat_follow": False,
                "enable_group_interjection": False,
            },
            settings={
                "group_wakeup_legacy_targeting": True,
                "group_wakeup_question_enabled": False,
                "group_wakeup_cold_group_enabled": True,
                "group_wakeup_cold_group_threshold": 80,
            },
        )
        with patch(
            "companion_v2.conversation.group_admission.LegacyGroupRuntime"
        ) as legacy:
            legacy.return_value.cold_ready.return_value = True
            self.assertEqual(
                admission.candidate(text="有人吗", **options), "cold_group"
            )
            self.assertEqual(admission.candidate(text="大家早上好", **options), "")
            self.assertEqual(
                admission.candidate(text="今天这件事真不错", **options), ""
            )

    def test_audit_compares_persistence_and_keeps_unrelated_features_active(self):
        config = {
            "daily_creative_config": {
                "enable_creative_writing": False,
                "creative_chars_per_session": 500,
                "enable_daily_greetings": True,
            },
            "news_config": {"news_share_probability": 1.0},
        }
        schema = {
            group: {
                "items": {
                    key: {"type": "bool" if type(value) is bool else "int"}
                    for key, value in values.items()
                }
            }
            for group, values in config.items()
        }
        source = "news_config.news_share_probability"
        migration = ConfigurationMigration(
            {"proactive.contact.news_share_probability": 1.0},
            {},
            {"proactive.contact.news_share_probability": (source,)},
            (),
        )
        result = strict_configuration_audit(
            config,
            schema,
            migrations={"private": migration},
            persisted_values={
                "private": {"proactive.contact.news_share_probability": 0.0}
            },
            verified_sources=(source,),
        )
        rows = {row["source"]: row for row in result["fields"]}
        self.assertEqual(rows[source]["reason"], "persisted_value_difference")
        self.assertEqual(
            rows["daily_creative_config.creative_chars_per_session"]["status"],
            "inactive",
        )
        self.assertEqual(
            rows["daily_creative_config.enable_daily_greetings"]["status"], "blocked"
        )
        result = strict_configuration_audit(
            config,
            schema,
            migrations={"private": migration},
            persisted_values={"private": migration.changes},
        )
        self.assertEqual(result["mapping_checks"], {"matched": 1})
        self.assertFalse(result["ready"])
        self.assertEqual(
            next(row for row in result["fields"] if row["source"] == source)["reason"],
            "mapped_runtime_unverified",
        )
        partial = ConfigurationMigration(
            migration.changes,
            {},
            migration.sources,
            (
                ConfigurationDifference(
                    "proactive.contact.news_share_probability",
                    "partial_mapping",
                    "",
                    source,
                ),
            ),
        )
        result = strict_configuration_audit(
            config, schema, migrations={"private": partial}, pending_sources=(source,)
        )
        self.assertEqual(
            next(row for row in result["fields"] if row["source"] == source)["reason"],
            "runtime_work_pending",
        )
        result = strict_configuration_audit(
            config,
            schema,
            verified_sources=(source,),
            runtime_differences={source: ("known_consumer_difference",)},
        )
        self.assertEqual(
            next(row for row in result["fields"] if row["source"] == source)["reason"],
            "runtime_behavior_difference",
        )
        self.assertFalse(result["ready"])


class NewsProbabilityMigrationTests(
    ContactScheduleFixture, unittest.IsolatedAsyncioTestCase
):
    def test_group_settings_consumer_accepts_migrated_daily_thirty(self):
        fields = {
            field.field_id: NS(
                value=30
                if field.field_id.endswith(".group_interject_max_daily")
                else field.default
            )
            for field in participation_fields()
        }
        with patch.object(
            GroupParticipationConfigurationOwner, "read", return_value=NS(fields=fields)
        ):
            settings = group_participation_settings(
                self.runtime, self.contact_context()
            )
        self.assertEqual(settings["group_interject_max_daily"], 30)

    async def test_news_probability_endpoints_reach_candidate_generation(self):
        article = {
            "id": "one",
            "title": "News",
            "url": "https://news.example/one",
            "summary": "Summary",
            "source": "Fixture",
            "published_at": (self.clock.now() - timedelta(minutes=1)).isoformat(),
        }
        port = NewsPort(self.clock, [[article]])
        host, batch = await self.scheduled(news_port=port)
        app, context = host.application, self.context(host.application, batch)
        self.settings(
            app, {"news_share_enabled": True, "news_share_probability": 0.0}
        ).unwrap()
        events = app.contact_schedule.news_events
        self.assertIsNone(
            events.select(context, self.preferences(app, context), self.clock.now())
        )
        self.settings(app, {"news_share_probability": 1.0}).unwrap()
        opportunity, marker = events.select(
            context, self.preferences(app, context), self.clock.now()
        )
        with patch(
            "companion_v2.proactive.news_events.random.random",
            side_effect=AssertionError("endpoint draw"),
        ):
            result = await events.prepare(context, marker)
        self.assertIsNotNone(opportunity)
        self.assertIsNotNone(result)


if __name__ == "__main__":
    unittest.main()

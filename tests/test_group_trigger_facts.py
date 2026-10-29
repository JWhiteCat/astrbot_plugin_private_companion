"""QQ raw recipients retain the same addressing boundaries as native mentions."""

import unittest
from types import SimpleNamespace as NS

from astrbot_host.group_admission import group_trigger_facts


def official_event(
    *, raw=None, parts=(), text="", platform="qq_official", self_id="qq_official"
):
    """Build a native event without importing the AstrBot runtime.

    Args:
        raw: Original platform payload.
        parts: Adapted native message components.
        text: Plain event text used when the payload has no content.
        platform: Adapter family name.
        self_id: Native bot account identifier.

    Returns:
        Minimal event accepted by the group trigger adapter.
    """
    return NS(
        platform_meta=NS(name=platform),
        message_obj=NS(raw_message=raw, message=list(parts), self_id=self_id),
        message_str=text,
        is_at_or_wake_command=False,
    )


class GroupTriggerFactsTests(unittest.TestCase):
    def test_raw_mapping_and_sdk_mentions_preserve_explicit_identity(self):
        for platform in ("qq_official", "qq_official_webhook"):
            for as_object in (False, True):
                for is_you in (False, True):
                    with self.subTest(
                        platform=platform, as_object=as_object, is_you=is_you
                    ):
                        mention = {
                            "id": "opaque-openid",
                            "username": "Member",
                            "is_you": is_you,
                        }
                        raw = (
                            NS(mentions=[NS(**mention)])
                            if as_object
                            else {"mentions": [mention]}
                        )
                        facts = group_trigger_facts(
                            official_event(raw=raw, platform=platform), "test"
                        )
                        self.assertEqual(facts["mentioned"], is_you)
                        self.assertEqual(facts["mentions_others"], not is_you)

    def test_sdk_raw_data_recovers_openid_aliases_and_content(self):
        for field in ("id", "member_openid", "user_openid"):
            with self.subTest(field=field):
                raw = NS(
                    mentions=[],
                    raw_data={
                        "mentions": [{field: "member-1", "is_you": False}],
                        "content": "请看一下",
                    },
                )
                facts = group_trigger_facts(official_event(raw=raw), "test")
                self.assertTrue(facts["mentions_others"])
                self.assertFalse(facts["mentioned"])

    def test_official_text_formats_and_fallbacks_recover_other_recipients(self):
        for token in (
            "<@MEMBER_1-a>",
            "<@!MEMBER_1-a>",
            '<qqbot-at-user id="MEMBER_1-a"/>',
            "<qqbot-at-user id='MEMBER_1-a' />",
        ):
            for source in ("mapping", "sdk", "raw_data", "event"):
                with self.subTest(token=token, source=source):
                    content = token + " 谁知道如何修复这个错误？"
                    raw = {
                        "mapping": {"content": content},
                        "sdk": NS(content=content),
                        "raw_data": NS(raw_data={"content": content}),
                        "event": None,
                    }[source]
                    event = official_event(
                        raw=raw, text=content if source == "event" else ""
                    )
                    facts = group_trigger_facts(event, "test")
                    self.assertFalse(facts["mentioned"])
                    self.assertTrue(facts["mentions_others"])

    def test_bot_identity_supports_account_self_id_and_official_placeholder(self):
        for target in ("test", "bot-openid", "qq_official"):
            with self.subTest(target=target):
                raw = {"content": "<@" + target + "> 帮我看看"}
                facts = group_trigger_facts(
                    official_event(raw=raw, self_id="bot-openid"), "test"
                )
                self.assertTrue(facts["mentioned"])
                self.assertFalse(facts["mentions_others"])

    def test_raw_metadata_false_beats_matching_bot_id_and_nickname(self):
        for target in ("test", "qq_official", "other-member"):
            with self.subTest(target=target):
                raw = {
                    "mentions": [
                        {"id": target, "username": "qq_official", "is_you": False}
                    ],
                    "content": "<@" + target + "> 谁知道怎么修复？",
                }
                facts = group_trigger_facts(official_event(raw=raw), "test")
                self.assertFalse(facts["mentioned"])
                self.assertTrue(facts["mentions_others"])

    def test_nickname_and_nonboolean_metadata_do_not_invent_bot_identity(self):
        for is_you in (None, "true", "false", 1, 0):
            with self.subTest(is_you=is_you):
                raw = NS(
                    mentions=[
                        NS(id="other-member", username="qq_official", is_you=is_you)
                    ]
                )
                facts = group_trigger_facts(official_event(raw=raw), "test")
                self.assertFalse(facts["mentioned"])
                self.assertTrue(facts["mentions_others"])

    def test_structured_bot_and_raw_other_mentions_are_both_preserved(self):
        for parts in (
            (NS(type="At", qq="qq_official"),),
            ({"type": "at", "data": {"qq": "qq_official"}},),
        ):
            with self.subTest(parts=parts):
                raw = {
                    "mentions": [{"id": "member-1", "is_you": False}],
                    "content": "<@member-1>",
                }
                facts = group_trigger_facts(
                    official_event(raw=raw, parts=parts), "test"
                )
                self.assertTrue(facts["mentioned"])
                self.assertTrue(facts["mentions_others"])

    def test_raw_bot_and_structured_other_mentions_are_both_preserved(self):
        raw = {
            "mentions": [{"id": "opaque-bot", "is_you": True}],
            "content": "<@opaque-bot>",
        }
        facts = group_trigger_facts(
            official_event(raw=raw, parts=[NS(type="At", qq="member-1")]), "test"
        )
        self.assertTrue(facts["mentioned"])
        self.assertTrue(facts["mentions_others"])

    def test_all_mention_and_native_wakeup_keep_their_existing_meaning(self):
        event = official_event(parts=[NS(type="At", qq="all")])
        event.is_at_or_wake_command = True
        facts = group_trigger_facts(event, "test")
        self.assertTrue(facts["mentions_all"])
        self.assertTrue(facts["native_wakeup"])
        self.assertFalse(facts["mentioned"])
        self.assertFalse(facts["mentions_others"])
        event.message_obj.raw_message = {
            "mentions": [{"id": "other-member", "is_you": False}]
        }
        self.assertTrue(group_trigger_facts(event, "test")["native_wakeup"])

    def test_other_adapters_do_not_interpret_official_text_or_metadata(self):
        for platform in ("aiocqhttp", "discord", ""):
            with self.subTest(platform=platform):
                event = official_event(
                    platform=platform,
                    raw={
                        "content": "<@other-member>",
                        "mentions": [{"id": "opaque-bot", "is_you": True}],
                    },
                )
                facts = group_trigger_facts(event, "test")
                self.assertFalse(facts["mentioned"])
                self.assertFalse(facts["mentions_others"])
                event.message_obj.message = [NS(type="At", qq="other-member")]
                self.assertTrue(group_trigger_facts(event, "test")["mentions_others"])

    def test_malformed_raw_mentions_do_not_break_group_admission(self):
        for mentions in (None, "bad", {}, [None, 123, {}, {"username": "qq_official"}]):
            with self.subTest(mentions=mentions):
                facts = group_trigger_facts(
                    official_event(raw={"mentions": mentions, "content": "普通群聊"}),
                    "test",
                )
                self.assertFalse(facts["mentioned"])
                self.assertFalse(facts["mentions_others"])

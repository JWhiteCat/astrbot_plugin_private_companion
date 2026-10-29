"""Group trigger facts from the authenticated native message components."""

import re
from collections.abc import Mapping


def group_trigger_facts(event, account_id):
    """Read group addressing facts without losing QQ official mention targets.

    Args:
        event: Incoming AstrBot event and its original platform payload.
        account_id: Configured bot account identity.

    Returns:
        Mention, native wakeup, quote, and image facts for group admission.
    """
    message = event.message_obj
    raw = getattr(message, "raw_message", None)
    segments = raw.get("message") if isinstance(raw, Mapping) else None
    parts = segments if isinstance(segments, list) else getattr(message, "message", ())
    platform_name = str(
        getattr(getattr(event, "platform_meta", None), "name", "") or ""
    )
    bot_ids = {str(account_id)}
    if platform_name in {"qq_official", "qq_official_webhook"}:
        bot_ids.update(
            value
            for value in (str(getattr(message, "self_id", "") or ""), "qq_official")
            if value
        )
    targets = {}
    mentions_all, has_image, replies = False, False, []
    for part in parts:
        if isinstance(part, Mapping):
            kind, data = part.get("type"), part.get("data", {})
            if not isinstance(data, Mapping):
                continue
            qq, identifier = data.get("qq"), data.get("id")
        else:
            kind = getattr(
                getattr(part, "type", None), "value", getattr(part, "type", None)
            )
            qq, identifier = getattr(part, "qq", None), getattr(part, "id", None)
        if kind in ("at", "At"):
            if str(qq) == "all":
                mentions_all = True
            elif qq is not None and str(qq).strip():
                targets[str(qq)] = str(qq) in bot_ids
        elif kind in ("reply", "Reply") and identifier is not None:
            replies.append(str(identifier))
        elif kind in ("image", "Image"):
            has_image = True
    if platform_name in {"qq_official", "qq_official_webhook"}:
        # QQ adapters may leave other members' mentions in the original
        # metadata or text instead of creating At components for them.
        raw_data = raw if isinstance(raw, Mapping) else getattr(raw, "raw_data", {})
        mentions = (
            raw.get("mentions")
            if isinstance(raw, Mapping)
            else getattr(raw, "mentions", None)
        )
        if not mentions and isinstance(raw_data, Mapping):
            mentions = raw_data.get("mentions")
        for mention in mentions if isinstance(mentions, (list, tuple)) else ():
            if isinstance(mention, Mapping):
                target = (
                    mention.get("id")
                    or mention.get("member_openid")
                    or mention.get("user_openid")
                )
                is_you = mention.get("is_you")
            else:
                target = (
                    getattr(mention, "id", None)
                    or getattr(mention, "member_openid", None)
                    or getattr(mention, "user_openid", None)
                )
                is_you = getattr(mention, "is_you", None)
            target = str(target or "").strip()
            if not target:
                continue
            if target == "all":
                mentions_all = True
            else:
                # Explicit platform identity overrides display names and any
                # provisional classification of the same component target.
                targets[target] = (
                    is_you if isinstance(is_you, bool) else target in bot_ids
                )
        content = (
            raw.get("content")
            if isinstance(raw, Mapping)
            else getattr(raw, "content", None)
        )
        if not content and isinstance(raw_data, Mapping):
            content = raw_data.get("content")
        content = str(
            content
            or getattr(event, "message_str", "")
            or getattr(message, "message_str", "")
            or ""
        )
        for match in re.finditer(
            r"<@!?([A-Za-z0-9_-]+)>|<qqbot-at-user\s+id=[\"']([A-Za-z0-9_-]+)[\"']\s*/>",
            content,
        ):
            target = match.group(1) or match.group(2)
            if target == "all":
                mentions_all = True
            else:
                targets.setdefault(target, target in bot_ids)
    mentioned = any(targets.values())
    mentions_others = any(not is_bot for is_bot in targets.values())
    quoted_bot = any(
        getattr(getattr(part, "type", None), "value", getattr(part, "type", None))
        == "Reply"
        and str(getattr(part, "id", "")) in replies
        and str(getattr(part, "sender_id", "")) == account_id
        for part in getattr(message, "message", ())
    )
    # is_wake also means an event listener activated, so it is not a reply trigger.
    return {
        "mentioned": mentioned,
        "native_wakeup": getattr(event, "is_at_or_wake_command", False) is True,
        "mentions_others": mentions_others,
        "reply_ids": tuple(dict.fromkeys(replies)),
        "mentions_all": mentions_all,
        "quoted_bot": quoted_bot,
        "has_image": has_image,
    }


def mentioned_account_ids(event):
    """Read @ targets from the authenticated native component chain."""
    message = getattr(getattr(event, "message_obj", None), "message", ())
    found = []
    for part in message or ():
        if isinstance(part, Mapping):
            kind, data = part.get("type"), part.get("data", {})
            target = data.get("qq") if isinstance(data, Mapping) else None
        else:
            kind = getattr(
                getattr(part, "type", None), "value", getattr(part, "type", None)
            )
            target = getattr(part, "qq", None)
        if kind not in ("at", "At") or target is None:
            continue
        value = str(target)
        if value.isascii() and value.isdecimal():
            found.append(value)
    return tuple(dict.fromkeys(found))

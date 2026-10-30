"""Read the old plugin's private users, independently of the host session list."""

import hashlib
import json

from .private_memory import moment
from .private_authorization import private_authorization, merge_authorization


SOURCE_ID = "private.core_profiles.users"
GENERIC_NAMES = frozenset(("用户", "主人", "主要用户", "默认用户", "临时会话"))
DELIVERY_UMO_FIELDS = (
    "umo",
    "bound_delivery_umo",
    "preferred_delivery_umo",
    "last_inbound_umo",
)


def private_umo(user):
    return next(
        (
            str(user.get(field) or "")
            for field in DELIVERY_UMO_FIELDS
            if user.get(field)
        ),
        "",
    )


def verified_private_identity(user):
    """Return (platform, account, participant) only for an identity V1 recorded as a private route.

    Relationship data is written for the same person card, so it must pass the
    same identity check. A number that lacks its adapter, bot account, supported
    platform kind or matching private UMO is never guessed into a V2 subject.
    """
    participant = user.get("identity_subject_id")
    platform, account = (
        user.get("identity_adapter_instance_id"),
        user.get("identity_bot_id"),
    )
    if not all(
        type(value) is str and value.strip()
        for value in (participant, platform, account)
    ) or user.get("identity_platform_kind") not in (
        "onebot",
        "aiocqhttp",
        "qq_official",
        "qq_official_webhook",
    ):
        return None
    umo = private_umo(user)
    if umo and umo not in (
        f"{platform}:FriendMessage:{participant}",
        f"{platform}:PrivateMessage:{participant}",
    ):
        return None
    return platform, account, participant


def native_private_identity(runtime, platform, participant, *, umo=""):
    """Read host-recorded native identity evidence for one exact private route.

    Args:
        runtime: Destination runtime containing accepted native event metadata.
        platform: Exact AstrBot platform instance identifier.
        participant: Native private user identifier, including opaque QQ IDs.
        umo: Optional exact source UMO, which must name the same private route.

    Returns:
        Native evidence, or an empty mapping when no exact proof exists.
    """
    expected = f"{platform}:FriendMessage:{participant}"
    if umo and umo != expected:
        return {}
    row = runtime.read_one(
        "SELECT value FROM runtime_metadata WHERE key=?",
        ("external_memory_identity:" + expected,),
    )
    if row is None:
        return {}
    try:
        evidence = json.loads(row["value"])
    except (ValueError, TypeError):
        return {}
    return evidence if isinstance(evidence, dict) else {}


def private_account_matches(runtime, context, profile):
    """Match platform accounts separately from QQ's native bot identifier.

    Args:
        runtime: Destination runtime holding native identity evidence.
        context: Authenticated destination scope.
        profile: Legacy private contact or relationship profile.

    Returns:
        Whether the exact native identity proves this account mapping.
    """
    if profile.get("platform") != context.scope.platform:
        return False
    if profile.get("platform_kind") not in ("qq_official", "qq_official_webhook"):
        return profile.get("account_id") == context.scope.account_id
    participant = profile["native_account_id"]
    proof = native_private_identity(
        runtime, profile["platform"], participant, umo=profile.get("private_umo", "")
    )
    return (
        proof.get("bot_id") == profile.get("account_id")
        and proof.get("platform") == profile["platform"]
        and proof.get("account_id") == context.scope.account_id
        and proof.get("user_id") == participant
        and proof.get("group_id") == ""
    )


def private_contacts(payload, *, v1_configuration=None):
    users = payload.get("users", {})
    if not isinstance(users, dict):
        return (), ({"reason": "users_invalid"},)
    found, rejected = {}, []
    for key, user in users.items():
        if not isinstance(user, dict):
            rejected.append({"source_key": str(key), "reason": "user_invalid"})
            continue
        capabilities = user.get("unified_profile_capabilities") or {}
        umo = private_umo(user)
        group_only = (
            user.get("profile_origin") == "group_observation"
            or user.get("observation_only")
            or isinstance(capabilities, dict)
            and capabilities.get("grant_source") == "legacy_effective_migration"
            and not umo
            and not user.get("last_seen")
        )
        if group_only:
            rejected.append({"source_key": str(key), "reason": "group_observation"})
            continue
        count = user.get("private_inbound_count", 0)
        count = count if type(count) is int and count >= 0 else 0
        maintained = user.get("manual_enabled") is True
        if not maintained and not count:
            rejected.append({"source_key": str(key), "reason": "no_private_history"})
            continue
        identity = verified_private_identity(user)
        if identity is None:
            rejected.append(
                {"source_key": str(key), "reason": "private_identity_unverified"}
            )
            continue
        platform, account, participant = identity
        nickname = " ".join(str(user.get("nickname") or "").split())[:64]
        if nickname in GENERIC_NAMES or nickname == participant:
            nickname = ""
        # last_seen also contains group activity in old stores. Only a dedicated
        # private timestamp can be displayed as the last private interaction.
        last_seen = moment(user.get("last_private_inbound_at"))
        item = {
            "source_id": SOURCE_ID,
            "platform": platform,
            "account_id": account,
            "platform_kind": user["identity_platform_kind"],
            "private_umo": umo,
            "native_account_id": participant,
            "nickname": nickname,
            "maintained": maintained,
            "message_count": count,
            "last_seen": last_seen or None,
            "authorization": private_authorization(key, user, v1_configuration),
        }
        identity = platform, account, participant
        previous = found.get(identity)
        if previous is not None:
            # Alias records are one user. Preserve the stronger factual counter,
            # while conflicting names never become trusted identity or grants.
            item["message_count"] = max(count, previous["message_count"])
            item["maintained"] |= previous["maintained"]
            item["nickname"] = previous["nickname"] or nickname
            item["authorization"] = merge_authorization(
                previous["authorization"], item["authorization"]
            )
        found[identity] = item
    items = []
    for identity in sorted(found):
        item = found[identity]
        item["fingerprint"] = hashlib.sha256(
            json.dumps(
                item, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()
        items.append(item)
    return tuple(items), tuple(rejected)

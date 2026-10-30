"""Read the old plugin's per-person relationship details without guessing identity.

V1 kept three pieces of relationship data on each user record: ``nickname``,
``persona_relationship.level`` and ``persona_relationship.note``. 它还有一份
配置级「用户档案」提示词（性别、生日、职业、关系定义、相处方式），那份由
``config_relationship_profile`` 单独读取，不会和用户记录混在一起。

The mapping is read-only. Identity fields are reused from the private-contact
converter (same platform, same bot account, same native user id, same supported
platform kind and matching private UMO); when they are missing or inconsistent
the record still appears with an ``issues`` entry so the migration preview can
say "needs confirmation" instead of guessing a subject.
"""

from .private_contacts import GENERIC_NAMES, private_umo, verified_private_identity


SOURCE_ID = "private.core_profiles.users"
MAPPED_FIELDS = {
    "nickname": "default_nickname",
    "persona_relationship.level": "role_relation",
    "persona_relationship.note": "extra",
}

# V1 还有一份「用户档案」提示词（life_schedule_config.roleplay_user_profile_prompt），
# 里面写着主要陪伴对象的称呼、性别、生日、职业、关系定义和相处方式。用户库里没有
# 这些字段，但它们确实在旧版配置里；只读用户库会让关系档案的这几项永远是空的。
PROMPT_SOURCE = "life_schedule_config.roleplay_user_profile_prompt"
PROMPT_LABELS = {
    "对用户的称呼": "default_nickname",
    "称呼": "default_nickname",
    "用户性别": "user_gender",
    "用户生日": "user_birthday",
    "用户职业": "user_occupation",
    "是角色的xx": "role_relation",
    "是角色的XX": "role_relation",
    "与角色的相处方式": "interaction",
    "相处方式": "interaction",
    "其他补充信息": "extra",
    "其它补充信息": "extra",
}


def _text(value):
    if not isinstance(value, str):
        return ""
    return value.replace("\r\n", "\n").replace("\r", "\n").strip()


def _label(value):
    return " ".join(_text(value).split())


def _mapped_values(user):
    """Return (values, source_fields, warnings) for the three relationship fields."""
    values, source_fields, warnings = {}, [], []
    participant = user.get("identity_subject_id")
    nickname = _label(user.get("nickname"))
    if nickname and (nickname in GENERIC_NAMES or nickname == participant):
        nickname = ""
    parsed = {"nickname": nickname}
    raw_relation = user.get("persona_relationship")
    relation = raw_relation if isinstance(raw_relation, dict) else {}
    if raw_relation not in (None, {}) and not isinstance(raw_relation, dict):
        warnings.append("persona_relationship_invalid")
    parsed["persona_relationship.level"] = _label(relation.get("level"))
    parsed["persona_relationship.note"] = _text(relation.get("note"))
    for source, name in MAPPED_FIELDS.items():
        value = parsed[source]
        if not value:
            continue
        values[name] = value
        source_fields.append(source)
    return values, source_fields, warnings


def _item(user_id, values, source_fields, warnings=()):
    return {
        "source_id": SOURCE_ID,
        "user_id": user_id,
        "platform": None,
        "account_id": None,
        "native_account_id": None,
        "nickname": values.get("default_nickname", ""),
        "role_relation": values.get("role_relation", ""),
        "extra": values.get("extra", ""),
        "source_fields": list(source_fields),
        "warnings": list(warnings),
        "issues": [],
    }


def _merge(previous, current):
    """Alias records are one person: keep the first non-empty value, never invent one."""
    for name in ("nickname", "role_relation", "extra"):
        if not previous[name]:
            previous[name] = current[name]
    previous["source_fields"] = list(dict.fromkeys((*previous["source_fields"], *current["source_fields"])))
    previous["warnings"] = list(dict.fromkeys((*previous["warnings"], *current["warnings"])))
    previous["user_ids"] = sorted({*previous.get("user_ids", [previous["user_id"]]),
                                   current["user_id"]})
    previous["user_id"] = previous["user_ids"][0]


def relationship_profiles(payload):
    """Map V1 ``users`` into V2 relationship-profile candidates and rejected rows."""
    users = payload.get("users", {}) if isinstance(payload, dict) else {}
    if not isinstance(users, dict):
        return (), ({"reason": "users_invalid"},)
    by_identity, unverified, rejected = {}, [], []
    for key, user in users.items():
        if not isinstance(user, dict):
            rejected.append({"source_key": str(key), "reason": "user_invalid"})
            continue
        values, source_fields, warnings = _mapped_values(user)
        if not values:
            rejected.append({"source_key": str(key), "reason": "relationship_fields_empty"})
            continue
        identity = verified_private_identity(user)
        if identity is None:
            item = _item(str(key), values, source_fields, warnings)
            item["issues"] = ["private_identity_unverified"]
            unverified.append(item)
            continue
        platform, account, participant = identity
        item = _item(str(key), values, source_fields, warnings)
        item.update({"platform": platform, "account_id": account,
                     "platform_kind": user["identity_platform_kind"], "private_umo": private_umo(user),
                     "native_account_id": participant})
        previous = by_identity.get(identity)
        if previous is None:
            by_identity[identity] = item
        else:
            _merge(previous, item)
    items = sorted(by_identity.values(),
                   key=lambda item: (item["platform"], item["account_id"], item["native_account_id"]))
    items.extend(sorted(unverified, key=lambda item: item["user_id"]))
    return tuple(items), tuple(rejected)


def _prompt_values(text):
    """把「标签：值」形式的关系档案提示词拆成字段；续行并入上一个字段。"""
    fields, order, current = {}, [], ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        label, separator, value = line.partition("：")
        if not separator:
            label, separator, value = line.partition(":")
        label = label.strip().strip("*# ").strip()
        if separator and label in PROMPT_LABELS:
            current = PROMPT_LABELS[label]
            if current not in order:
                order.append(current)
            fields[current] = value.strip()
            continue
        if current:
            fields[current] = (fields[current] + "\n" + line).strip() if fields[current] else line
    return fields, order


def config_relationship_profile(text, *, primary_user_id=""):
    """从旧版配置的「用户档案」提示词解析主要陪伴对象的关系资料。

    返回 ``{"user_id", "fields", "dropped", "source_fields"}``，没有可迁移内容时
    返回 None。字段超过 V2 上限就整项丢弃（由迁移预览显示为 dropped），不截断。
    """
    from companion_v2.relationships.profile import FIELD_LIMITS as RELATIONSHIP_LIMITS

    if not isinstance(text, str) or not text.strip():
        return None
    values, order = _prompt_values(text)
    fields, dropped = {}, []
    for name in order:
        value = values.get(name, "").strip()
        if not value:
            continue
        if len(value) > RELATIONSHIP_LIMITS[name]:
            dropped.append(name)
            continue
        fields[name] = value
    if not fields and not dropped:
        return None
    return {
        "user_id": str(primary_user_id or "").strip(),
        "fields": fields,
        "dropped": dropped,
        "source_fields": [f"{PROMPT_SOURCE}：{label}" for label, name in PROMPT_LABELS.items()
                          if name in fields],
    }

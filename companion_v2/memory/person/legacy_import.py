"""Import reviewed private contact summaries without replaying events or grants."""

import json

from companion_v2.execution.models import TaskError, encode, stamp
from companion_v2.legacy.private_contacts import private_account_matches
from .access import OWNER, card_id, owner_for_subject, resolve_subject
from .models import KIND, empty_document
from .service import UPSERT


def contact_plan(runtime, context, profiles):
    ready, present, pending, excluded = [], [], [], []
    for profile in profiles:
        if not private_account_matches(runtime, context, profile):
            excluded.append({"native_account_id": profile["native_account_id"], "reason": "different_account"})
            continue
        try:
            subject = resolve_subject(runtime, context, profile["native_account_id"])
        except TaskError:
            pending.append({"native_account_id": profile["native_account_id"], "reason": "identity_not_registered"})
            continue
        owner = owner_for_subject(context, subject)
        row = runtime.read_one("SELECT state, document_json FROM memory_views WHERE view_id = ? AND kind = ?",
                               (card_id(owner), KIND))
        document = json.loads(row["document_json"]) if row else {}
        entry = {"subject_id": subject, "native_account_id": profile["native_account_id"],
            "nickname": profile["nickname"], "maintained": profile["maintained"],
            "message_count": profile["message_count"]}
        if row and row["state"] != "active":
            excluded.append({**entry, "reason": "target_inactive"})
        elif document.get("legacy_private_contact"):
            present.append(entry)
        else:
            ready.append(entry)
    return {"source_id": "private.core_profiles.users", "total": len(profiles),
        "ready": len(ready), "already_present": len(present),
        "maintained_count": sum(item["maintained"] for item in (*ready, *present)),
        "samples": ready + present, "pending": pending, "excluded": excluded}


def import_contacts(runtime, context, profiles, at):
    plan = contact_plan(runtime, context, profiles)
    by_id = {item["native_account_id"]: item for item in profiles
             if private_account_matches(runtime, context, item)}
    created = 0
    with runtime.transaction() as connection:
        for entry in plan["samples"]:
            subject = entry["subject_id"]
            owner = owner_for_subject(context, subject)
            identifier = card_id(owner)
            row = connection.execute("SELECT * FROM memory_views WHERE view_id = ? AND kind = ?",
                                     (identifier, KIND)).fetchone()
            document = json.loads(row["document_json"]) if row else empty_document()
            if row and row["state"] != "active" or document.get("legacy_private_contact"):
                continue
            scope = context.scope
            document["route"] = document.get("route") or {"installation_id": scope.installation_id,
                "platform": scope.platform, "bot_id": scope.bot_id, "account_id": scope.account_id,
                "role_id": context.role_binding.role.subject_id, "subject_id": subject}
            document["native_account_id"] = document.get("native_account_id") or entry["native_account_id"]
            document["legacy_private_contact"] = dict(by_id[entry["native_account_id"]])
            connection.execute(UPSERT, (identifier, owner, KIND, row["revision"] + 1 if row else 1,
                encode(document), encode({"owner": OWNER}), stamp(at)))
            created += 1
    return {"created": created, "already_present": plan["already_present"],
        "pending": plan["pending"], "excluded": plan["excluded"], "remaining": 0,
        "permissions_restored": False, "execution_restored": False}

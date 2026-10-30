"""Restore reviewed legacy permissions once, using registered native identities."""

import json

from companion_v2.execution.models import TaskError, digest, encode, require, stamp
from companion_v2.foundation.refs import ScopeKind, SubjectKind, SubjectRef
from companion_v2.legacy.private_contacts import private_account_matches
from .grants import PERSON_PERMISSION_GRANT_PREFIX
from .models import AuthorizationTier, GrantState
from .object_access import ObjectAccessPolicy
from .personas import ConversationRoute, PersonaDirectory
from .repository import PersistentIdentityDirectory


PREFIX = "legacy.private-access:"


def _subject(runtime, context, native_id, at):
    directory = PersistentIdentityDirectory(runtime)
    subjects = set()
    for binding in directory.snapshot().bindings:
        principal = binding.principal
        if (principal.platform, principal.bot_id, principal.account_id, principal.native_account_id) != (
                context.scope.platform, context.scope.bot_id, context.scope.account_id, native_id):
            continue
        resolved = directory.resolve_authenticated(principal, observed_at=at)
        if resolved.is_success and resolved.unwrap().subject.kind is SubjectKind.HUMAN:
            subjects.add(resolved.unwrap().subject.subject_id)
    if len(subjects) != 1:
        raise TaskError("legacy_authorization_identity_unverified", "the legacy user has no unique registered identity")
    return next(iter(subjects))


def _key(scope, role_id, native_id):
    return PREFIX + digest([scope.installation_id, scope.platform, scope.bot_id,
                            scope.account_id, role_id, native_id])


def _read(runtime, key):
    row = runtime.read_one("SELECT value FROM runtime_metadata WHERE key = ?", (key,))
    return json.loads(row["value"]) if row else None


def _write(runtime, key, value):
    with runtime.transaction() as connection:
        connection.execute("INSERT INTO runtime_metadata(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, encode(value)))


def _scopes(runtime, context, subject, at):
    result = []
    directory = PersonaDirectory(runtime)
    for row in runtime.read_all("SELECT record_json FROM conversation_routes", ()):
        route = ConversationRoute.restore(json.loads(row["record_json"]))
        scope = route.scope
        if (scope.kind is not ScopeKind.PRIVATE or route.user != subject or not scope.is_resolved
                or (scope.platform, scope.bot_id, scope.account_id) !=
                   (context.scope.platform, context.scope.bot_id, context.scope.account_id)):
            continue
        binding = directory.resolve(scope, observed_at=at)
        if binding.is_success and binding.unwrap().binding.role == context.role_binding.role:
            result.append(scope)
    return result


def authorization_plan(runtime, context, profiles, at):
    entries, pending = [], []
    identity = PersistentIdentityDirectory(runtime)
    for profile in profiles:
        source = profile.get("authorization", {})
        if not source.get("tier") and not source.get("permissions"):
            continue
        if not private_account_matches(runtime, context, profile):
            continue
        native_id = profile["native_account_id"]
        try:
            subject_id = _subject(runtime, context, native_id, at)
        except TaskError:
            pending.append({"native_account_id": native_id, "reason": "identity_not_registered"})
            continue
        subject = SubjectRef(runtime.installation_id, SubjectKind.HUMAN, subject_id)
        key = _key(context.scope, context.role_binding.role.subject_id, native_id)
        stored = _read(runtime, key)
        tier = runtime.read_one("SELECT tier, revision FROM subject_access_tiers "
            "WHERE subject_kind = ? AND subject_id = ?", (subject.kind.value, subject.subject_id))
        scopes = _scopes(runtime, context, subject, at)
        grants = [grant for grant in identity.grants_for_subject(subject)
                  if grant.scope in scopes and grant.capability in source.get("permissions", {})]
        entries.append({"native_account_id": native_id, "subject_id": subject_id,
            "source": source, "receipt": stored, "current_tier": dict(tier) if tier else None,
            "scopes": [scope.document() for scope in scopes],
            "grants": [{"id": grant.grant_id, "revision": grant.revision, "state": grant.state.value}
                       for grant in sorted(grants, key=lambda item: item.grant_id)],
            "state": "present" if stored else "ready"})
    return {"ready": sum(item["state"] == "ready" for item in entries),
            "already_present": sum(item["state"] == "present" for item in entries),
            "core_users": sum(item["source"].get("tier") == "core" for item in entries),
            "permission_count": sum(len(item["source"].get("permissions", {})) for item in entries),
            "deferred": sum(not item["scopes"] and item["state"] == "ready" for item in entries),
            "pending": pending, "entries": entries}


def _apply_permissions(runtime, subject, scope, source, at, key):
    identity = PersistentIdentityDirectory(runtime)
    applied, preserved = 0, 0
    for capability, enabled in source.get("permissions", {}).items():
        grants = [grant for grant in identity.grants_for_subject(subject)
                  if grant.scope == scope and grant.capability == capability]
        # Active bootstrap defaults are replaceable. A later operator decision,
        # including revocation or expiry, always wins over the legacy snapshot.
        if any(not grant.grant_id.startswith("host-multi-grant:")
               or grant.state is not GrantState.ACTIVE or grant.expires_at is not None for grant in grants):
            preserved += 1
            continue
        for grant in grants:
            identity.revoke_grant(grant.grant_id, revoked_at=at, expected_revision=identity.revision)
        identifier = PERSON_PERMISSION_GRANT_PREFIX + digest([key, scope.key, capability])
        identity.issue_grant(grant_id=identifier, subject=subject, scope=scope, capability=capability,
            tier=AuthorizationTier.STANDARD, valid_from=at, expected_revision=identity.revision)
        if not enabled:
            identity.revoke_grant(identifier, revoked_at=at, expected_revision=identity.revision)
        applied += 1
    return applied, preserved


def import_authorizations(runtime, context, profiles, at):
    plan = authorization_plan(runtime, context, profiles, at)
    tiers, permissions, preserved, deferred = 0, 0, 0, 0
    policy = ObjectAccessPolicy(runtime)
    with runtime.transaction():
        for entry in plan["entries"]:
            if entry["state"] != "ready":
                continue
            subject = SubjectRef(runtime.installation_id, SubjectKind.HUMAN, entry["subject_id"])
            source = entry["source"]
            key = _key(context.scope, context.role_binding.role.subject_id, entry["native_account_id"])
            if source.get("tier") and entry["current_tier"] is None:
                policy.set_tier(subject, AuthorizationTier(source["tier"]),
                    expected_revision=policy.revision, observed_at=at)
                tiers += 1
            elif source.get("tier"):
                preserved += 1
            scopes = _scopes(runtime, context, subject, at)
            for scope in scopes:
                changed, kept = _apply_permissions(runtime, subject, scope, source, at, key)
                permissions += changed
                preserved += kept
            complete = bool(scopes) or not source.get("permissions")
            deferred += not complete
            _write(runtime, key, {"source": source, "subject_id": subject.subject_id,
                "confirmed_at": stamp(at), "permissions_applied": complete})
    return {"identity_restored": tiers, "permissions_restored": permissions,
            "preserved": preserved, "deferred": deferred,
            "already_present": plan["already_present"], "pending": plan["pending"]}


def apply_pending_private_authorization(runtime, actor, scope, role_id, at):
    if scope.kind is not ScopeKind.PRIVATE:
        return
    key = _key(scope, role_id, actor.principal.native_account_id)
    with runtime.transaction():
        record = _read(runtime, key)
        if not record or record["permissions_applied"] or record["subject_id"] != actor.subject.subject_id:
            return
        current = require(PersistentIdentityDirectory(runtime).resolve_authenticated(actor.principal, observed_at=at))
        role = require(PersonaDirectory(runtime).resolve(scope, observed_at=at)).binding.role
        if current.subject != actor.subject or role.subject_id != role_id:
            raise TaskError("legacy_authorization_target_changed", "the reviewed private identity or role changed")
        _apply_permissions(runtime, actor.subject, scope, record["source"], at, key)
        _write(runtime, key, {**record, "permissions_applied": True})

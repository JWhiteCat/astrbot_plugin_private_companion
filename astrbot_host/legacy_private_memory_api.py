"""Dashboard migration of an exact V1 person into the selected private role."""

import json

from astrbot.api.web import request

from companion_v2.execution.models import digest, encode, stamp
from companion_v2.foundation.outcome import Outcome
from companion_v2.legacy.private_memory import build_private_memory_plan
from companion_v2.legacy.private_contacts import native_private_identity
from companion_v2.memory.private_legacy_import import (
    import_private_continuity,
    import_private_memory,
)


class PrivateMemoryMigrationAPI:
    def __init__(self, legacy):
        self.legacy = legacy

    def routes(self, prefix):
        return [
            (
                prefix + "/private-memory-plan",
                self.plan,
                ["GET"],
                "Preview V1 person-private historical memory",
            ),
            (
                prefix + "/private-memory-apply",
                self.apply,
                ["POST"],
                "Import the selected person's V1 private history",
            ),
        ]

    def _plan(self, migration, root, session):
        context, _ = self.legacy._data_context(session)
        if getattr(context.scope.kind, "value", context.scope.kind) != "private":
            return Outcome.failed(
                "legacy_private_memory_private_target_required",
                "请选择对应的 V2 私聊角色。",
            )
        source = build_private_memory_plan(root, migration.v1_configuration)
        if source.source_error:
            return Outcome.failed(source.source_error, "V1 本体私聊记忆来源暂不可读。")
        records = source.matching(
            account_id=context.scope.account_id,
            native_actor_id=context.actor.principal.native_account_id,
            platform=context.scope.platform,
            native_identity=native_private_identity(
                session.application.runtime,
                context.scope.platform,
                context.actor.principal.native_account_id,
            ),
        )
        target = self.legacy._memory_target(context)
        token = digest(
            [source.source_fingerprint, target, [r["person_id"] for r in records]]
        )
        result = source.document()
        result.update(
            {
                "plan_token": token,
                "matched_person_count": len(records),
                "matched_material_count": sum(len(r["materials"]) for r in records),
                "requires_role_confirmation": True,
                "destination": {
                    "account_id": context.scope.account_id,
                    "participant_id": context.actor.principal.native_account_id,
                    "role_id": context.role_binding.role.subject_id,
                },
            }
        )
        return Outcome.success((result, source, records, target))

    async def plan(self):
        from .web_api import outcome_response

        if set(request.query.keys()) - {"v1_root"}:
            return outcome_response(
                Outcome.failed("legacy_request_invalid", "invalid private memory query")
            )

        def work(migration, root, session):
            result = self._plan(migration, root, session)
            return Outcome.success(result.unwrap()[0]) if result.is_success else result

        return await self.legacy._run(
            work, root_override=request.query.get("v1_root"), offload_sync=True
        )

    async def apply(self):
        from .web_api import outcome_response

        try:
            raw = await request.body()
            if len(raw) > 8192:
                raise ValueError()
            arguments = json.loads(raw.decode("utf-8"))
            if (
                not isinstance(arguments, dict)
                or set(arguments) - {"plan_token", "confirmed", "v1_root"}
                or arguments.get("confirmed") is not True
                or not isinstance(arguments.get("plan_token"), str)
            ):
                raise ValueError()
        except (ValueError, UnicodeError):
            return outcome_response(
                Outcome.failed(
                    "legacy_request_invalid", "请确认旧记忆属于当前私聊角色。"
                )
            )

        def work(migration, root, session):
            planned = self._plan(migration, root, session)
            if not planned.is_success:
                return planned
            document, source, records, target = planned.unwrap()
            if document["plan_token"] != arguments["plan_token"]:
                return Outcome.failed(
                    "legacy_plan_stale", "来源或目标已变化，请重新试算。"
                )
            if not records:
                return Outcome.failed(
                    "legacy_private_memory_identity_unmatched",
                    "没有与当前账号及私聊对象对应的 V1 本体记忆。",
                )
            runtime = session.application.runtime
            store = session.application.memory.query.legacy
            has_continuity = any("legacy_state" in record for record in records)
            if has_continuity:
                from .management_identity import dashboard_operator

                context, _ = self.legacy._data_context(session)
                operator = dashboard_operator(
                    runtime, request.username, at=self.legacy.controller.now()
                )
                import_private_continuity(
                    store,
                    source,
                    records,
                    context,
                    observed_at=self.legacy.controller.now(),
                    operator=operator,
                    preview=True,
                )
            receipt_key = "legacy_private_memory_import:" + digest(target)
            pending = {
                "at": stamp(self.legacy.controller.now()),
                "source_root": self.legacy._migration_root_identity(root),
                "requested_source_fingerprint": source.source_fingerprint,
                "state": "applying",
                "cutover_ready": False,
            }
            # The two databases cannot commit atomically. Persist an incomplete
            # marker first so a crash cannot masquerade as completed migration.
            with runtime.transaction() as connection:
                connection.execute(
                    "INSERT INTO runtime_metadata(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (receipt_key, encode(pending)),
                )
            try:
                with runtime.transaction() as connection:
                    continuity = (
                        import_private_continuity(
                            store,
                            source,
                            records,
                            context,
                            observed_at=self.legacy.controller.now(),
                            operator=operator,
                        )
                        if has_continuity
                        else None
                    )
                    result = import_private_memory(store, source, records, target)
                    if continuity is not None:
                        result["continuity"] = continuity
                        result["continuity_blockers"] = continuity[
                            "continuity_blockers"
                        ]
                        result["cutover_ready"] = (
                            not continuity["continuity_blockers"]
                            and not source.rejected
                        )
                    connection.execute(
                        "UPDATE runtime_metadata SET value=? WHERE key=?",
                        (
                            encode({**pending, **result, "state": "completed"}),
                            receipt_key,
                        ),
                    )
            except Exception as error:
                with runtime.transaction() as connection:
                    connection.execute(
                        "UPDATE runtime_metadata SET value=? WHERE key=?",
                        (
                            encode(
                                {
                                    **pending,
                                    "state": "failed",
                                    "code": getattr(
                                        error, "code", "legacy_import_incomplete"
                                    ),
                                }
                            ),
                            receipt_key,
                        ),
                    )
                raise
            return Outcome.success(result)

        return await self.legacy._run(
            work, root_override=arguments.get("v1_root"), offload_sync=True
        )

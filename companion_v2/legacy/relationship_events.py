"""Verified v6.5.6 relationship events over the candidate's durable receipts."""

from datetime import datetime, timezone

from companion_v2.execution.models import stamp
from .legacy_relationship_ledger import (
    _finite_number,
    _integer,
    apply_relationship_event,
)


def settle_proactive_reply(
    connection, context, document, *, event_id, observed_at, score_options
):
    """Settle the first accepted input after a confirmed proactive delivery.

    The v6.5.6 message_pipeline.py branches at lines 442 and 854 settle inbound
    first, then add two points and clear awaiting_reply_since, even when capped.
    A durable complete delivery replaces that old successful-send marker. Drafts,
    partial sends and uncertain receipts cannot establish the marker.

    Args:
        connection: Caller-owned transaction in the destination runtime.
        context: Exact authenticated private account, participant and role.
        document: Mutable imported continuity document for that native identity.
        event_id: Trusted native input identity, never text or a model assertion.
        observed_at: Timestamp of the accepted input.
        score_options: Validated original ledger limits and resolved time zone.

    Returns:
        Original ledger receipt for the reply bonus, or None without a pending send.
    """
    interaction = document.setdefault("relationship_interaction", {})
    seen = interaction.setdefault("processed_inbound_events", [])
    if event_id in seen:
        return None
    seen.append(event_id)
    del seen[:-200]
    prior_input = _finite_number(document.get("last_activity_at")) or 0.0
    pending = (_finite_number(interaction.get("awaiting_reply_since")) or 0.0) > 0
    # Scope columns in the original request survive grant/binding revisions;
    # all native ownership dimensions still have to match this imported account.
    row = connection.execute(
        """SELECT d.delivery_id, d.delivered_at
        FROM background_deliveries d JOIN proactive_contacts c
          ON json_extract(d.request_json, '$.source_ref') = c.contact_id
          AND c.origin_key = d.origin_key
        WHERE d.owner = 'proactive.contact' AND d.state = 'delivered'
          AND d.delivered_at >= ? AND d.delivered_at <= ?
          AND json_extract(d.request_json, '$.context.audience.scope.installation_id') = ?
          AND json_extract(d.request_json, '$.context.audience.scope.platform') = ?
          AND json_extract(d.request_json, '$.context.audience.scope.account_id') = ?
          AND json_extract(d.request_json, '$.context.audience.scope.bot_id') = ?
          AND json_extract(d.request_json, '$.context.audience.scope.native_scope_id') = ?
          AND json_extract(d.request_json, '$.context.audience.scope.kind') = 'private'
          AND json_extract(d.request_json, '$.context.role_binding.role.subject_id') = ?
          AND json_extract(d.request_json, '$.context.actor.subject.subject_id') = ?
          AND json_extract(d.request_json, '$.context.actor.principal.native_account_id') = ?
          AND json_extract(d.request_json, '$.sequence') = json_extract(d.request_json, '$.part_count') - 1
          AND (SELECT COUNT(*) FROM background_deliveries p
               WHERE p.owner = d.owner AND p.origin_key = d.origin_key
                 AND json_extract(p.request_json, '$.source_ref') = c.contact_id
                 AND p.state = 'delivered') = json_extract(d.request_json, '$.part_count')
          AND NOT EXISTS (SELECT 1 FROM background_deliveries p
               WHERE p.owner = d.owner AND p.origin_key = d.origin_key
                 AND json_extract(p.request_json, '$.source_ref') = c.contact_id
                 AND p.state != 'delivered')
        ORDER BY d.delivered_at DESC, d.delivery_id DESC LIMIT 1""",
        (
            stamp(datetime.fromtimestamp(prior_input, timezone.utc)),
            stamp(observed_at),
            context.scope.installation_id,
            context.scope.platform,
            context.scope.account_id,
            context.scope.bot_id,
            context.scope.native_scope_id,
            context.role_binding.role.subject_id,
            context.actor.subject.subject_id,
            context.actor.principal.native_account_id,
        ),
    ).fetchone()
    if row and row["delivery_id"] != interaction.get("last_replied_delivery_id"):
        pending = True
        interaction["last_replied_delivery_id"] = row["delivery_id"]
    if not pending:
        return None
    receipt = apply_relationship_event(
        document["relationship"], 2, reason_code="proactive_reply", **score_options
    )
    interaction["awaiting_reply_since"] = 0
    interaction["reply_count"] = (_integer(interaction.get("reply_count")) or 0) + 1
    interaction["last_reply_at"] = observed_at.timestamp()
    interaction["last_private_reply_at"] = observed_at.timestamp()
    return receipt

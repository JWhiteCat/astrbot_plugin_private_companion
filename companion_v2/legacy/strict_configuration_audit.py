"""Fail-closed field accounting for an explicitly requested equivalent upgrade."""

from collections import Counter

from .configuration_migration import (
    _source_value,
    configuration_audit_sources,
    configuration_rules,
    plugin_configuration_rules,
    semantic_configuration_rules,
)


# Only whole features with independent, explicit master switches are listed.
# A disabled child option is never evidence that an unrelated feature is idle.
FEATURE_MASTERS = {
    "voice_action_config": ("enable_voice_action", "enable_tts_enhancement"),
    "voice_playback_config": ("enable_tts_local_playback",),
    "auto_voice_config": ("auto_voice_enabled",),
    "reading_archive_config": ("enable_reading_archive_integration",),
    "balance_awareness_config": ("enable_balance_awareness",),
    "screen_action_config": ("enable_screen_glance_action",),
    "poke_action_config": ("enable_poke_action",),
    "reactive_poke_config": ("enable_reactive_poke",),
    "presence_sync_config": ("enable_qq_presence_sync",),
}

# These groups contain unrelated features, so their switches must never disable
# every sibling setting (for example, creative writing does not gate greetings).
INACTIVE_CHILDREN = {
    "daily_creative_config.enable_creative_writing": (
        "enable_creative_work_read_guard",
        "creative_inspiration_probability",
        "creative_share_probability",
        "creative_chars_per_session",
        "creative_max_active_projects",
        "creative_hidden_mode",
        "creative_direction_prompt",
    ),
    "private_followup_config.enable_group_conversation_followup": (
        "group_conversation_followup_seconds",
        "group_conversation_followup_max_turns",
    ),
    "private_followup_config.enable_proactive_burst": (
        "proactive_burst_max_messages",
        "proactive_burst_gap_min_seconds",
        "proactive_burst_gap_max_seconds",
    ),
    "recall_config.enable_forbidden_word_recall": (
        "recall_forbidden_words",
        "recall_forbidden_scope",
        "recall_forbidden_word_case_sensitive",
    ),
    "basic_config.enable_multi_persona_mode": ("multi_persona_ids",),
    "basic_config.enable_daily_token_soft_limit": ("daily_token_soft_limit",),
    "basic_config.enable_relationship_stage_provider_routing": (
        "relationship_stage_provider_routes",
    ),
    "basic_config.enable_worldview_perception": (
        "worldview_adaptation_mode",
        "worldview_adaptation_prompt",
    ),
    "model_assignment_config.enable_sensitive_model_replacement": (
        "SENSITIVE_REPLACEMENT_PROVIDER_ID",
        "sensitive_replacement_keywords",
    ),
    "message_debounce_config.enable_smart_message_debounce": (
        "smart_message_debounce_model_timeout_seconds",
        "smart_message_debounce_wait_seconds",
        "smart_message_debounce_learning_window_seconds",
        "smart_message_debounce_examples_limit",
    ),
    "weather_config.enable_weather_alerts": (
        "weather_alert_api_host",
        "weather_alert_token",
        "weather_alert_refresh_minutes",
        "weather_alert_min_severity",
        "weather_alert_api_key",
    ),
}


def strict_configuration_audit(
    configuration,
    schema,
    *,
    verified_sources=(),
    inactive_sources=(),
    migrations=None,
    persisted_values=None,
    pending_sources=(),
    runtime_differences=None,
):
    """Account for every schema leaf without equating a mapping with behavior proof.

    Args:
        configuration: Frozen original V1 configuration.
        schema: The schema shipped with that exact V1 version.
        verified_sources: Source paths with completed equivalence acceptance evidence.
        inactive_sources: Additional paths whose inactive parent conditions were verified.
        migrations: Named converter results generated from this exact frozen input.
        persisted_values: Optional named target dictionaries read after applying and
            reopening the same migration; omitted targets are never considered proved.
        pending_sources: Paths whose runtime equivalence is being implemented separately.
        runtime_differences: Verified behavioral differences keyed by canonical source.

    Returns:
        A value-free ledger, status totals, and a switch-over readiness flag.
        Every active unverified field blocks the switch, including default values.

    Raises:
        ValueError: A source or schema is not a mapping, or schema keys overlap.
    """
    if not isinstance(configuration, dict) or not isinstance(schema, dict):
        raise ValueError(
            "strict migration requires configuration and source schema objects"
        )
    verified, inactive, pending_sources = (
        set(verified_sources),
        set(inactive_sources),
        set(pending_sources),
    )
    audit = {entry["source"]: entry for entry in configuration_audit_sources()}
    rules = {}
    for rule in (
        *configuration_rules(),
        *plugin_configuration_rules(),
        *semantic_configuration_rules(),
    ):
        rules.setdefault(rule["source"], []).append(rule["target"])
    mappings, differences, catalog_findings = {}, {}, {}
    for name, migration in (migrations or {}).items():
        for target, sources in migration.sources.items():
            if target not in migration.changes:
                raise ValueError("migration source lacks its converted target")
            observed = (persisted_values or {}).get(name, {})
            check = (
                "not_checked"
                if persisted_values is None
                else "matched"
                if target in observed and observed[target] == migration.changes[target]
                else "mismatch"
                if target in observed
                else "missing"
            )
            for source in sources:
                mappings.setdefault(source, []).append(
                    {
                        "migration": name,
                        "target": target,
                        "contract_validated": True,
                        "persisted_value": check,
                    }
                )
        for difference in migration.differences:
            for source in difference.source_key.split("、"):
                if source:
                    destination = (
                        catalog_findings
                        if difference.audit_disposition
                        else differences
                    )
                    destination.setdefault(source, set()).add(difference.kind)
    fields, names, groups = [], set(), set()
    pending = []
    for group, definition in schema.items():
        if (
            isinstance(definition, dict)
            and isinstance(definition.get("items"), dict)
            and definition["items"]
        ):
            groups.add(group)
            pending.extend(
                (group + "." + name, field)
                for name, field in definition["items"].items()
            )
    canonical_sources = {source.rsplit(".", 1)[-1]: source for source, _ in pending}
    while pending:
        source, field = pending.pop()
        if not isinstance(field, dict):
            continue
        if (
            field.get("type") == "object"
            and isinstance(field.get("items"), dict)
            and field["items"]
        ):
            pending.extend(
                (source + "." + name, item) for name, item in field["items"].items()
            )
            continue
        group, leaf = source.split(".", 1)[0], source.rsplit(".", 1)[-1]
        if leaf in names:
            raise ValueError("source schema contains ambiguous leaf aliases")
        names.add(leaf)
        present, value = _source_value(configuration, source)
        default = field.get(
            "default",
            {
                "bool": False,
                "int": 0,
                "float": 0.0,
                "list": [],
                "template_list": [],
                "object": "",
            }.get(field.get("type"), ""),
        )
        masters = FEATURE_MASTERS.get(group, ())
        disabled_parent = (
            bool(masters)
            and leaf not in masters
            and all(
                _source_value(configuration, group + "." + master) == (True, False)
                for master in masters
            )
        )
        inactive_parents = (
            [group + "." + key for key in masters] if disabled_parent else []
        )
        for parent, children in INACTIVE_CHILDREN.items():
            if source == parent.rsplit(".", 1)[0] + "." + leaf and leaf in children:
                if _source_value(configuration, parent) == (True, False):
                    inactive_parents.append(parent)
        if source in (
            "group_observation_config.group_whitelist_ids",
            "legacy_compat_config.target_group_ids",
        ) and _source_value(
            configuration, "group_observation_config.group_access_mode"
        ) == (True, "blacklist"):
            inactive_parents.append(
                "group_observation_config.group_access_mode=blacklist"
            )
        disabled_parent = bool(inactive_parents)
        condition = field.get("condition", {})
        if isinstance(condition, str):
            condition = {condition: True}
        condition_results, condition_sources = [], []
        if isinstance(condition, dict):
            for key, expected in condition.items():
                parent = canonical_sources.get(key)
                if parent is None:
                    condition_results.append(None)
                    continue
                parent_present, parent_value = _source_value(configuration, parent)
                condition_sources.append(parent)
                condition_results.append(
                    None
                    if not parent_present
                    else parent_value in expected
                    if isinstance(expected, list)
                    else parent_value == expected
                )
        # Schema display conditions identify work to inspect; they do not prove
        # the runtime stopped reading a value, so cannot open the strict gate.
        schema_condition = (
            False
            if False in condition_results
            else None
            if None in condition_results
            else True
        )
        actual_mappings = mappings.get(source, [])
        kinds = sorted(differences.get(source, ()))
        material = set(kinds) - {
            "group_precedence",
            "inactive_source",
            "derived_default",
            "already_represented",
            "host_managed",
        }
        persistence_failed = any(
            item["persisted_value"] in ("mismatch", "missing")
            for item in actual_mappings
        )
        reason = (
            "missing_source"
            if not present
            else "inactive_parent"
            if disabled_parent or source in inactive
            else "persisted_value_difference"
            if persistence_failed
            else "runtime_behavior_difference"
            if source in (runtime_differences or {})
            else "runtime_work_pending"
            if source in pending_sources
            else "conversion_difference"
            if material
            else "equivalence_verified"
            if source in verified
            else "mapped_runtime_unverified"
            if actual_mappings
            else "schema_inactive_runtime_unverified"
            if schema_condition is False
            else "catalog_runtime_unverified"
            if source in catalog_findings
            else "host_runtime_unverified"
            if "host_managed" in kinds
            else "unsupported_or_unverified_runtime"
        )
        status = (
            "missing"
            if not present
            else "inactive"
            if disabled_parent or source in inactive
            else "verified"
            if reason == "equivalence_verified"
            else "blocked"
        )
        fields.append(
            {
                "source": source,
                "status": status,
                "present": present,
                "nondefault": present and value != default,
                "reason": reason,
                "targets": rules.get(source, []),
                "mappings": actual_mappings,
                "differences": kinds,
                "catalog_findings": sorted(catalog_findings.get(source, ())),
                "schema_condition_satisfied": schema_condition,
                "schema_condition_sources": condition_sources,
                "runtime_differences": list(
                    (runtime_differences or {}).get(source, ())
                ),
                "audit_disposition": audit.get(source, {}).get("verdict", "unreviewed"),
                "inactive_parent": inactive_parents,
            }
        )
    fields.sort(key=lambda row: row["source"])
    # Undocumented values are not silently exempted merely because the normal
    # migration catalog has never heard of them. Flat aliases and group objects
    # are accounted for by the canonical leaf above.
    extras = [
        {
            "source": key,
            "status": "verified"
            if key in verified
            else "inactive"
            if key in inactive
            else "blocked",
        }
        for key in sorted(configuration)
        if key not in names and key not in groups
    ]
    totals = dict(Counter(row["status"] for row in (*fields, *extras)))
    return {
        "schema": 1,
        "field_count": len(fields),
        "fields": fields,
        "extra_fields": extras,
        "totals": totals,
        "reason_totals": dict(Counter(row["reason"] for row in fields)),
        "mapping_checks": dict(
            Counter(
                item["persisted_value"] for row in fields for item in row["mappings"]
            )
        ),
        "ready": not any(
            row["status"] in ("missing", "blocked") for row in (*fields, *extras)
        ),
    }


def audit_frozen_configuration(
    configuration,
    schema,
    *,
    registry,
    accounts=None,
    verified_sources=(),
    inactive_sources=(),
    pending_sources=(),
    persisted_values=None,
    runtime_differences=None,
):
    """Run the real converters and retain a secret-free, exhaustive evidence ledger.

    Args:
        configuration: Frozen V1 configuration, including grouped precedence.
        schema: Matching V1 schema.
        registry: Registry constructed by the real V2 application.
        accounts: Verified native account/persona inventory; None defers routing proof.
        verified_sources: Completed runtime equivalence acceptance evidence.
        inactive_sources: Additional independently checked inactive branches.
        pending_sources: Source behavior assigned to another acceptance check.
        persisted_values: Values reread after migration and restart, keyed by converter
            name: registry_private, registry_group, plugin_private, plugin_group,
            semantic, or account. No values are included in the returned report.
        runtime_differences: Verified remaining source behavior differences.

    Returns:
        Field ledger, actual conversion findings, and strict readiness decision.
    """
    from .configuration_migration import (
        _ACCOUNT_CONFIGURATION_SOURCES,
        convert_account_configuration,
        convert_configuration,
        convert_plugin_configuration,
        convert_semantic_configuration,
    )
    from companion_v2.execution.models import digest

    migrations = {
        "registry_" + kind: convert_configuration(
            configuration, registry=registry, window_kind=kind
        )
        for kind in ("private", "group")
    }
    migrations.update(
        {
            "plugin_" + kind: convert_plugin_configuration(
                configuration, window_kind=kind
            )
            for kind in ("private", "group")
        }
    )
    migrations["semantic"] = convert_semantic_configuration(configuration)
    pending = set(pending_sources)
    if accounts is None:
        pending.update(_ACCOUNT_CONFIGURATION_SOURCES)
    else:
        migrations["account"] = convert_account_configuration(
            configuration, accounts=accounts
        )
    result = strict_configuration_audit(
        configuration,
        schema,
        verified_sources=verified_sources,
        inactive_sources=inactive_sources,
        migrations=migrations,
        persisted_values=persisted_values,
        pending_sources=pending,
        runtime_differences=runtime_differences,
    )
    result["source_config_digest"] = digest(configuration)
    result["converters"] = {
        name: {
            "converted_targets": len(migration.changes),
            "difference_kinds": dict(
                Counter(row.kind for row in migration.differences)
            ),
        }
        for name, migration in migrations.items()
    }
    result["account_inventory_checked"] = accounts is not None
    return result

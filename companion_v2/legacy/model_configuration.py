"""Convert old route policies only onto registered V2 model task profiles."""

import json
from copy import deepcopy


def expand_quick_model_configuration(config):
    """Project V1 quick routes before registry, plugin, and semantic conversion.

    Args:
        config: Original grouped or flat V1 settings; never modified.

    Returns:
        A detached effective configuration, or the original non-quick input.
    """
    from .configuration_migration import (
        _source_value,
        configuration_rules,
        plugin_configuration_rules,
        semantic_configuration_rules,
    )

    if not isinstance(config, dict):
        return config
    present, mode = _source_value(
        config, "model_assignment_config.provider_config_mode"
    )
    if not present or mode != "quick":
        return config
    values = {}
    for key in (
        "FAST_RESPONSE_PROVIDER_ID",
        "COMPLEX_REASONING_PROVIDER_ID",
        "CREATIVE_MODEL_PROVIDER_ID",
    ):
        _, raw = _source_value(config, "model_assignment_config." + key)
        values[key] = raw.strip() if isinstance(raw, str) else ""
    fast = values["FAST_RESPONSE_PROVIDER_ID"]
    complex_model = values["COMPLEX_REASONING_PROVIDER_ID"]
    creative = values["CREATIVE_MODEL_PROVIDER_ID"]
    routes = {}
    for key in (
        "LLM_PROVIDER_ID",
        "DAILY_PLAN_PROVIDER_ID",
        "DETAIL_ENHANCEMENT_PROVIDER_ID",
        "HISTORY_SUMMARY_PROVIDER_ID",
        "RELATIONSHIP_ANALYSIS_PROVIDER_ID",
        "COMPANION_MEMORY_PROVIDER_ID",
        "DIALOGUE_EPISODE_PROVIDER_ID",
        "GROUP_EPISODE_PROVIDER_ID",
        "FORWARD_MESSAGE_PROVIDER_ID",
        "PROACTIVE_PERSONA_JUDGE_PROVIDER_ID",
        "TROUBLESHOOTING_PROVIDER_ID",
        "DAILY_REVIEW_PROVIDER_ID",
    ):
        routes[key] = complex_model
    for key in (
        "MAI_STYLE_PROVIDER_ID",
        "RESPONSE_REVIEW_PROVIDER_ID",
        "SMART_SILENCE_PROVIDER_ID",
        "EMOTION_JUDGEMENT_PROVIDER_ID",
        "SMART_MESSAGE_DEBOUNCE_PROVIDER_ID",
        "REST_WAKEUP_PROVIDER_ID",
        "GROUP_INTERJECT_PROVIDER_ID",
        "GROUP_SLANG_PROVIDER_ID",
        "VOICE_PROMPT_PROVIDER_ID",
        "tts_conversion_provider_id",
        "NARRATION_PROVIDER_ID",
        "NEWS_PROVIDER_ID",
        "WEB_EXPLORATION_PROVIDER_ID",
    ):
        routes[key] = fast or complex_model
    routes["GROUP_FOLLOWUP_JUDGE_PROVIDER_ID"] = fast
    for key in (
        "CREATIVE_PROVIDER_ID",
        "CREATIVE_OUTLINE_PROVIDER_ID",
        "CREATIVE_REVIEW_PROVIDER_ID",
        "DREAM_DIARY_PROVIDER_ID",
        "PHOTO_PROMPT_PROVIDER_ID",
    ):
        routes[key] = creative or complex_model
    result = deepcopy(config)
    result.update(routes)
    result.setdefault("model_assignment_config", {}).update(routes)
    for rule in (
        *configuration_rules(),
        *plugin_configuration_rules(),
        *semantic_configuration_rules(),
    ):
        path = rule["source"].split(".")
        if len(path) == 2 and path[-1] in routes:
            result.setdefault(path[0], {})[path[-1]] = routes[path[-1]]
    return result


def append_model_configuration(config, registry, candidates, differences):
    from .configuration_migration import ConfigurationDifference, _source_value, _boolean
    from companion_v2.model_gateway.replacement_rules import checked_rules
    from companion_v2.model_gateway.refusal import parse_refusal_terms

    prefix = "model_assignment_config."
    fields = {field.group_id for field in registry.fields.values()
              if field.field_id.startswith("model.") and field.field_id.endswith(".provider")}
    def add(group, suffix, value, source):
        field_id = group + "." + suffix
        field = registry.fields.get(field_id)
        if field is None:
            return
        try:
            value = field.validate(value)
            if suffix == "replacement_rules":
                checked_rules(value)
            elif suffix == "refusal_terms":
                parse_refusal_terms(value)
            elif suffix == "replacement_scope" and value not in ("all", "plugin", "conversation"):
                raise ValueError()
        except (ValueError, TypeError):
            differences.append(ConfigurationDifference(field_id, "invalid_value",
                "旧模型策略不符合当前任务合同，保留原文归档供核对。", source))
            return
        candidates.setdefault(field_id, []).append(({"source":source,"confidence":"high"}, value))

    for name, suffix in (("model_replacement_rules", "replacement_rules"),
                         ("model_replacement_scope", "replacement_scope"),
                         ("enable_sensitive_model_replacement", "fallback_on_refusal"),
                         ("SENSITIVE_REPLACEMENT_PROVIDER_ID", "fallback"),
                         ("sensitive_replacement_keywords", "refusal_terms")):
        source = prefix + name
        present, value = _source_value(config, source)
        if not present:
            continue
        if suffix == "fallback_on_refusal":
            try:
                value = _boolean(value)
            except ValueError:
                continue
        if suffix == "replacement_rules" and isinstance(value, list):
            value = json.dumps(value,ensure_ascii=False,separators=(",",":"))
        if suffix == "refusal_terms" and isinstance(value, list) and all(isinstance(i,str) for i in value):
            value = "\n".join(value)
        if suffix == "fallback" and not value:
            continue
        # V1 conversation replacements apply to conversational work, not image/audio providers.
        if "model.conversation.reply" in fields:
            differences.append(ConfigurationDifference("model.conversation.reply." + suffix, "host_managed",
                "普通对话的模型选路、拒答回退与超时由 AstrBot 当前会话管理；插件默认策略只供插件附加任务使用。", source))

    timeouts = config.get("model_timeout_overrides", {})
    if isinstance(timeouts, dict):
        for task, value in timeouts.items():
            group = task if task.startswith("model.") else "model." + task
            if group in fields and group != "model.conversation.reply":
                add(group, "timeout_seconds", value, "model_timeout_overrides")
            else:
                differences.append(ConfigurationDifference(group + ".timeout_seconds", "host_managed" if group == "model.conversation.reply" else "unavailable",
                    "此旧任务由宿主管理或没有对应的 V2 任务档；超时保留在原配置中。", "model_timeout_overrides"))

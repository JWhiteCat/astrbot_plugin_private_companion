"""Read external memory for V2 model calls that bypass native request hooks."""

import asyncio
from dataclasses import replace

from companion_v2.conversation.foreground import PromptMaterial
from companion_v2.execution.models import TaskError

from .memory_owner import (
    MEMORY_OWNER_PLUGIN_ID,
    memory_owner_provider,
    native_memory_identity,
    read_memory_owner,
)


async def with_external_memory(context, bridge, envelope):
    """Attach one scoped external recall to an authenticated model request.

    Args:
        context: Native AstrBot context holding the registered memory provider.
        bridge: Original conversation bridge used to verify persona and account.
        envelope: Sealed V2 request whose model transport bypasses native hooks.

    Returns:
        The request with bounded external memory, or the original request when
        the installation uses local memory or the provider returns no matches.

    Raises:
        TaskError: The selected provider or its scoped contract is unavailable.
    """
    runtime = getattr(bridge, "memory_runtime", None)
    selected = memory_owner_provider(context, runtime=runtime).state()
    if not selected.delegated:
        return envelope
    owner = read_memory_owner(context)
    if owner.handle is None or not owner.ownership.delegated:
        raise TaskError(
            "external_memory_unavailable", "the selected memory provider is unavailable"
        )
    compose = getattr(owner.handle, "compose_context", None)
    if not callable(compose):
        raise TaskError(
            "external_memory_contract_unavailable",
            "the memory provider cannot compose context",
        )
    status_reader = getattr(owner.handle, "p5_capability_status", None)
    if callable(status_reader):
        status = status_reader()
        if isinstance(status, dict) and status.get("bridge_gate") is True:
            # V2 has no authenticated P5 issuer. Do not mistake the bridge's
            # empty denial for a successful recall with no matching memories.
            raise TaskError(
                "external_memory_attestation_required",
                "external memory requires an authenticated P5 issuer",
            )
    request = envelope.original
    persona = await bridge.verified_persona(request.event.scope, request.role_binding)
    identity = native_memory_identity(
        context,
        request.event.scope,
        persona_id=str(persona.get("persona_id") or ""),
        runtime=runtime,
    )
    query = "\n".join(
        material.payload
        for material in request.context.materials
        if material.purpose in {"current_source", "background_trigger"}
    )
    query = (query or request.instruction)[:2000]
    try:
        recalled = await asyncio.wait_for(
            compose(query=query, session_context=identity, top_k=6, max_chars=4000),
            timeout=20,
        )
    except Exception as error:
        raise TaskError(
            "external_memory_recall_failed", "external memory recall did not complete"
        ) from error
    current = read_memory_owner(context)
    if current.handle is not owner.handle or not current.ownership.delegated:
        raise TaskError(
            "external_memory_owner_changed", "memory ownership changed during recall"
        )
    if not isinstance(recalled, str):
        raise TaskError(
            "external_memory_contract_invalid",
            "external memory returned an invalid context",
        )
    if not recalled.strip():
        return envelope
    materials = tuple(
        item
        for item in request.context.materials
        if item.owner != MEMORY_OWNER_PLUGIN_ID
    )
    materials += (
        PromptMaterial(MEMORY_OWNER_PLUGIN_ID, "external_memory", recalled[:4000]),
    )
    request = replace(request, context=replace(request.context, materials=materials))
    return replace(envelope, original=request)

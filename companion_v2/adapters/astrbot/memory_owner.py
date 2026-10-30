"""读「我会牢牢记住你」自己的声明，判定谁在对外提供记忆。

## 探测什么，不探测什么

牢插件已经在 :mod:`astrbot_plugin_remember_you.main` 里公开了取句柄的函数——
``get_memory_companion_bridge()`` 与 ``get_active_bridge()``——并且它的
``__init__`` 会在配置 ``private_companion_bridge.enabled`` 为假时把活动句柄置为
``None``、在 ``terminate()`` 里调用 ``handle.deactivate()``。所以「它在不在、它
有没有启用、它是否还在服务」这三件事都能从**它自己的声明**读出来，本模块因此
不去猜文件是否存在，也不去读它的数据库。

判定顺序（每一步失败都倒向「V2 自己来」，见
:mod:`companion_v2.capabilities.ownership`）：

1. 宿主插件注册表里那一条是否 ``activated``。插件被停用时模块可能还挂在
   ``sys.modules`` 上，只有注册表能说明它已经被停用——先看注册表就不会把停用的
   声明当成活的。
2. 取句柄函数存在否（``owner_declaration_absent``）。
3. 调用它。返回 ``None`` 就是声明说这个桥没启用（``owner_handle_disabled``）。
4. 句柄自报的生命周期。它提供 ``bridge_lifecycle_status()``，已停用时返回
   ``{"active": False}``（``owner_handle_unreadable``）。这个方法不存在或报了别的
   形态时**不**据此否决——老版本没有它，不能因为读不到状态就把一个在服务的记忆
   插件判死。

跨插件只读句柄和这个状态查询，**不**调用它任何会改状态的方法。

## 为什么只有这一个函数跨插件

``companion_v2`` 的核心不能反向导入宿主包，所以真实探测集中在这里的一个函数
里，由 ``astrbot_host`` 组装时注入。别家模块名变了、被卸载了、或者根本装的是别的
插件，影响面都只有这一个函数。

## 读判定与拿句柄是同一次探测

推送（:mod:`companion_v2.adapters.astrbot.memory_push`）必须复用这里的结果：判定
说「由别家提供」时，它要用的就是这一次读到的那个句柄。所以 :func:`read_memory_owner`
一次交回「判定 + 活句柄」，:func:`read_memory_ownership` 只是它的判定那一半——全仓
只有这一条发现路径，不写第二次 ``sys.modules`` 扫描，也不读第二遍注册表。
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging

from companion_v2.execution.models import TaskError

from companion_v2.capabilities.ownership import (
    MemoryOwnership,
    REASON_DECLARATION_ABSENT,
    REASON_EXTERNAL_ACTIVE,
    REASON_HANDLE_DISABLED,
    REASON_HANDLE_UNREADABLE,
    REASON_MODULE_ABSENT,
    RENDER_EXTERNAL,
    RENDER_SELF,
)


# 与 ``companion_page_bridge.COMPANION_MODULE_NAMES`` 同一套写法：先注册表给出来的
# 模块路径，再兜底 ``sys.modules`` 里的两种导入形态（AstrBot 用 ``data.plugins.``
# 前缀，独立运行时用裸包名）。这里不做 import：插件没加载时导入会执行它的模块级
# 代码，那属于「探测带来副作用」。
MEMORY_OWNER_MODULE_NAMES = (
    "data.plugins.astrbot_plugin_memory_companion.main",
    "astrbot_plugin_memory_companion.main",
    "data.plugins.astrbot_plugin_remember_you.main",
    "astrbot_plugin_remember_you.main",
)

# 声明自己是谁。``display_name`` 用于页面文案；模块没写 display_name 时用插件 ID
# 兜底，宁可显示机器名也不假装知道它叫什么。
MEMORY_OWNER_PLUGIN_ID = "astrbot_plugin_memory_companion"
EXTERNAL_MEMORY_OWNER_KEY = "external_memory_owner"
EXTERNAL_MEMORY_IDENTITY_PREFIX = "external_memory_identity:"
MEMORY_OWNER_DISPLAY_NAME = "我会牢牢记住你"
MEMORY_OWNER_ACCESSORS = ("get_memory_companion_bridge", "get_active_bridge")

# 插件注册表里可能用这几个字段之一表明身份，逐个比对（去掉连字符/下斜杠后比较）。
_MEMORY_OWNER_IDENTITIES = frozenset(
    {MEMORY_OWNER_PLUGIN_ID, "astrbot-plugin-memory-companion", "MemoryCompanion", "memory_companion"}
)


@dataclass(frozen=True)
class MemoryOwnerHandle:
    """一次探测的两半：归属判定，以及判定为「别家提供」时的那个活句柄。

    ``handle`` 只在 ``ownership.delegated`` 为真时非空。判定说自己不服务时把句柄
    一起交出去是危险的——调用方很容易顺手用一个已经被停用的桥去写别人的库。
    """

    ownership: MemoryOwnership
    handle: object | None = None


def _identity(value) -> str:
    try:
        return str(value or "").casefold().replace("-", "_").replace("/", ".")
    except Exception:
        return ""


def _matches(value) -> bool:
    text = _identity(value)
    if not text:
        return False
    parts = {part for part in text.split(".") if part}
    return bool(parts & {_identity(item) for item in _MEMORY_OWNER_IDENTITIES})


def _display_name(module) -> str:
    # 已知的展示名优先。牢插件顶层只有 PLUGIN_NAME / PLUGIN_VERSION，没有
    # PLUGIN_DISPLAY_NAME，所以只靠 getattr 会一路退到 module.__name__，于是页面
    # 把「我会牢牢记住你」显示成 ``astrbot_plugin_remember_you.main``。
    # 归属文案是给人看的，宁可写死已知的名字，也不把模块路径当名字。
    for value in (getattr(module, "PLUGIN_DISPLAY_NAME", None), getattr(module, "display_name", None)):
        text = str(value or "").strip()
        if text and text != MEMORY_OWNER_PLUGIN_ID:
            return text
    return MEMORY_OWNER_DISPLAY_NAME


def _handle_state(owner) -> MemoryOwnerHandle:
    """句柄已经拿到时的最后一步：它自己说还能不能服务。"""

    try:
        status = getattr(owner, "bridge_lifecycle_status", None)
    except Exception:
        status = None
    if callable(status):
        try:
            reported = status()
        except Exception:
            return MemoryOwnerHandle(MemoryOwnership(owner=RENDER_SELF, reason=REASON_HANDLE_UNREADABLE))
        if isinstance(reported, dict) and reported.get("active") is False:
            return MemoryOwnerHandle(MemoryOwnership(owner=RENDER_SELF, reason=REASON_HANDLE_UNREADABLE))
    return MemoryOwnerHandle(MemoryOwnership(owner=RENDER_EXTERNAL, reason=REASON_EXTERNAL_ACTIVE), owner)


def _from_module(module, *, module_name: str, reason_if_none: str) -> MemoryOwnerHandle:
    """只看声明的形状，不看异常正文：读不到就是读不到。"""

    accessor = None
    for name in MEMORY_OWNER_ACCESSORS:
        try:
            candidate = getattr(module, name, None)
        except Exception:
            candidate = None
        if callable(candidate):
            accessor = candidate
            break
    if accessor is None:
        return MemoryOwnerHandle(MemoryOwnership(owner=RENDER_SELF, reason=REASON_DECLARATION_ABSENT, module=module_name))
    try:
        owner = accessor()
    except Exception:
        return MemoryOwnerHandle(MemoryOwnership(owner=RENDER_SELF, reason=REASON_HANDLE_UNREADABLE, module=module_name))
    if owner is None:
        return MemoryOwnerHandle(MemoryOwnership(owner=RENDER_SELF, reason=reason_if_none,
                              module=module_name, provider=MEMORY_OWNER_PLUGIN_ID,
                              display_name=MEMORY_OWNER_DISPLAY_NAME))
    state = _handle_state(owner)
    if state.ownership.delegated:
        return MemoryOwnerHandle(MemoryOwnership(owner=RENDER_EXTERNAL, reason=REASON_EXTERNAL_ACTIVE, module=module_name,
                              provider=MEMORY_OWNER_PLUGIN_ID, display_name=_display_name(module)), owner)
    return MemoryOwnerHandle(MemoryOwnership(owner=RENDER_SELF, reason=state.ownership.reason, module=module_name,
                          provider=MEMORY_OWNER_PLUGIN_ID, display_name=_display_name(module)))


def _from_registry(context) -> MemoryOwnerHandle | None:
    """只认宿主注册表当前装载的那一个模块对象。

    注册表里那一条的 ``activated`` 是「插件是否启用」的权威回答：被停用的插件可能
    还把模块留在 ``sys.modules`` 上（甚至在别的导入名下面），所以停用那一条不能被
    模块扫描推翻——它直接给出 ``owner_handle_disabled``，与声明自己返回 ``None``
    同形，页面因此能说「装了但关着」，而不是含糊的「读不到」。
    """

    if context is None:
        return None
    try:
        entries = context.get_all_stars() if callable(getattr(context, "get_all_stars", None)) else None
    except Exception:
        entries = None
    for metadata in tuple(entries or ()):
        if not (_matches(getattr(metadata, "name", None)) or _matches(getattr(metadata, "module_path", None))
                or _matches(getattr(metadata, "root_dir_name", None))):
            continue
        if not bool(getattr(metadata, "activated", True)):
            return MemoryOwnerHandle(MemoryOwnership(owner=RENDER_SELF, reason=REASON_HANDLE_DISABLED,
                                  provider=MEMORY_OWNER_PLUGIN_ID, display_name=MEMORY_OWNER_DISPLAY_NAME))
        module = getattr(metadata, "module", None)
        if module is None:
            return None
        name = str(getattr(metadata, "module_path", None) or getattr(module, "__name__", "") or "")
        return _from_module(module, module_name=name, reason_if_none=REASON_HANDLE_DISABLED)
    return None


def _from_modules(seen: str) -> MemoryOwnerHandle:
    import sys

    for name in MEMORY_OWNER_MODULE_NAMES:
        if name == seen:
            # 注册表已经看过同一个模块对象了，再扫只会得到同一结论。
            continue
        module = sys.modules.get(name)
        if module is None:
            continue
        # 兜底路径里模块在、句柄为空只能是因为这个桥没启用：没有注册表说明它
        # 被停用，也没有别的证据。
        return _from_module(module, module_name=name, reason_if_none=REASON_HANDLE_DISABLED)
    return MemoryOwnerHandle(MemoryOwnership(owner=RENDER_SELF, reason=REASON_MODULE_ABSENT))


def read_memory_owner(context) -> MemoryOwnerHandle:
    """本进程内当前谁在向模型提供记忆，以及它在服务时的那个句柄。

    注册表是权威：它给出的模块对象就是宿主当前装载的那一个，所以只用它来读声明。
    ``sys.modules`` 只是兜底，而且只在注册表**说不出话**时才看——独立运行、或宿主
    没有注册表接口。这一步的顺序很重要：被停用的插件常常还留在 ``sys.modules``
    里，先扫模块就会把一条「已停用」的声明当成活的，V2 于是白白让位。

    任何一路失败都倒向 ``RENDER_SELF``：宁可两边都注入（看得见、可修），也不要
    两边都不说话。
    """

    registered = _from_registry(context)
    if registered is not None:
        if registered.ownership.delegated:
            return registered
        # 注册表看到了这个插件，并且给出了否定的结论（停用、句柄空、没有声明）：
        # 这就是第一手证据，不再看 ``sys.modules``。只有在注册表「没看到它」时，
        # 残留模块仍可能是一份真实声明，才轮到兜底扫描——而且它会跳过注册表已经
        # 看过的那个模块，不会把停用插件的残留模块当成活的。
        if registered.ownership.reason != REASON_MODULE_ABSENT:
            return registered
    modules = _from_modules(registered.ownership.module if registered is not None else "")
    if not modules.ownership.delegated and registered is not None:
        return registered
    return modules


def read_memory_ownership(context) -> MemoryOwnership:
    """本进程内当前谁在向模型提供记忆。只读，可反复调用，无副作用。

    判定就是 :func:`read_memory_owner` 的那一半；需要句柄的调用方（推送）用后者，
    免得为了拿句柄再探一次。
    """

    return read_memory_owner(context).ownership


def native_memory_identity(context, scope, *, persona_id, runtime=None):
    """Resolve the external memory identity from recorded native evidence.

    Args:
        context: Native AstrBot context used to verify the platform kind.
        scope: Authenticated V2 conversation scope.
        persona_id: Persona selected and verified by the native host.
        runtime: Optional installation store containing migrated owner evidence.

    Returns:
        Exact session, bot, persona and target fields for Memory Companion.

    Raises:
        TaskError: The original native identity is missing or no longer matches.
    """
    kind = scope.kind.value
    if kind not in {"private", "group"} or not persona_id:
        raise TaskError(
            "external_memory_identity_unavailable",
            "memory requires a native conversation and persona",
        )
    native_session = getattr(scope, "session_id", None) or scope.native_scope_id
    session_id = f"{scope.platform}:{'FriendMessage' if kind == 'private' else 'GroupMessage'}:{native_session}"
    bot_id = ""
    if runtime is not None:
        row = runtime.read_one(
            "SELECT value FROM runtime_metadata WHERE key = ?",
            (EXTERNAL_MEMORY_IDENTITY_PREFIX + session_id,),
        )
        if row is not None:
            try:
                evidence = json.loads(row[0])
            except (TypeError, ValueError) as error:
                raise TaskError(
                    "external_memory_identity_invalid",
                    "recorded memory identity is invalid",
                ) from error
            expected = {
                "persona_id": persona_id,
                "platform": scope.platform,
                "account_id": scope.account_id,
                "user_id": scope.native_scope_id if kind == "private" else "",
                "group_id": scope.native_scope_id if kind == "group" else "",
            }
            if not isinstance(evidence, dict) or any(
                evidence.get(key) != value for key, value in expected.items()
            ):
                raise TaskError(
                    "external_memory_identity_changed",
                    "native memory route differs from its recorded owner",
                )
            bot_id = str(evidence.get("bot_id") or "")
            if not bot_id:
                raise TaskError(
                    "external_memory_identity_invalid",
                    "recorded memory owner is missing",
                )
    if not bot_id:
        platform = context.get_platform_inst(scope.platform)
        if platform is not None and platform.meta().name == "aiocqhttp":
            bot_id = scope.account_id or ""
    if not bot_id:
        raise TaskError(
            "external_memory_identity_unavailable",
            "native memory bot identity has not been verified",
        )
    return {
        "session_id": session_id,
        "scope": kind,
        "platform": scope.platform,
        "bot_id": bot_id,
        "persona_id": persona_id,
        "user_id": scope.native_scope_id if kind == "private" else "",
        "group_id": scope.native_scope_id if kind == "group" else "",
    }


def memory_owner_provider(context=None, *, runtime=None):
    """组装用的窄端口：注入一个函数，核心只认 ``state()``。"""

    return _ContextMemoryOwner(context, runtime)


async def record_native_memory_identity(runtime, bridge, event, context):
    """Persist native ownership evidence after authenticated input admission.

    Args:
        runtime: Installation store holding the selected external owner.
        bridge: Native bridge that verifies the current persona.
        event: Original SDK event, never a reconstructed background event.
        context: Admitted conversation identity and audience.

    Returns:
        Whether exact native evidence was recorded. Missing evidence leaves
        background memory unavailable without invalidating admitted input.
    """
    if not memory_owner_provider(bridge.context, runtime=runtime).state().delegated:
        return False
    try:
        scope = context.scope
        channel = "FriendMessage" if scope.kind.value == "private" else "GroupMessage"
        native_session = getattr(scope, "session_id", None) or scope.native_scope_id
        umo = f"{scope.platform}:{channel}:{native_session}"
        if str(event.unified_msg_origin) != umo:
            raise TaskError(
                "external_memory_identity_changed",
                "native event route differs from its admitted scope",
            )
        bot_id = str(event.get_self_id() or "").strip()
        if not bot_id:
            raise TaskError(
                "external_memory_identity_unavailable",
                "native event has no memory owner identity",
            )
        persona = await bridge.verified_persona(scope, context.role_binding)
        persona_id = str(persona.get("persona_id") or "").strip()
        if not persona_id:
            raise TaskError(
                "external_memory_identity_unavailable",
                "native event has no verified persona",
            )
        evidence = {
            "bot_id": bot_id,
            "persona_id": persona_id,
            "platform": scope.platform,
            "account_id": scope.account_id,
            "user_id": scope.native_scope_id if scope.kind.value == "private" else "",
            "group_id": scope.native_scope_id if scope.kind.value == "group" else "",
        }
        with runtime.transaction() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO runtime_metadata (key, value) VALUES (?, ?)",
                (
                    EXTERNAL_MEMORY_IDENTITY_PREFIX + umo,
                    json.dumps(evidence, ensure_ascii=False),
                ),
            )
        return True
    except Exception as error:
        logging.getLogger(__name__).warning(
            "Native memory identity could not be recorded error_type=%s",
            type(error).__name__,
        )
        return False


class _ContextMemoryOwner:
    def __init__(self, context, runtime=None):
        self._context = context
        self._runtime = runtime

    def state(self) -> MemoryOwnership:
        state = read_memory_ownership(self._context)
        if self._runtime is not None:
            try:
                selected = self._runtime.read_one(
                    "SELECT value FROM runtime_metadata WHERE key = ?",
                    (EXTERNAL_MEMORY_OWNER_KEY,),
                )
            except Exception:
                # An unreadable installation cannot authorize a second live store.
                return MemoryOwnership(
                    owner=RENDER_EXTERNAL,
                    reason=REASON_HANDLE_UNREADABLE,
                    provider=MEMORY_OWNER_PLUGIN_ID,
                    display_name=MEMORY_OWNER_DISPLAY_NAME,
                )
            if selected is not None and selected[0]:
                # A migrated installation must not start a second live memory
                # store when its selected external owner is temporarily down.
                return MemoryOwnership(
                    owner=RENDER_EXTERNAL,
                    reason=state.reason,
                    module=state.module,
                    provider=str(selected[0]),
                    display_name=state.display_name or MEMORY_OWNER_DISPLAY_NAME,
                )
        return state


__all__ = [
    "EXTERNAL_MEMORY_IDENTITY_PREFIX",
    "EXTERNAL_MEMORY_OWNER_KEY",
    "MEMORY_OWNER_ACCESSORS",
    "MEMORY_OWNER_DISPLAY_NAME",
    "MEMORY_OWNER_MODULE_NAMES",
    "MEMORY_OWNER_PLUGIN_ID",
    "MemoryOwnerHandle",
    "memory_owner_provider",
    "native_memory_identity",
    "record_native_memory_identity",
    "read_memory_owner",
    "read_memory_ownership",
]

"""Startup-static plugin discovery, validation, activation, and disposal."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextvars import ContextVar
from dataclasses import dataclass
import heapq
import inspect
import re
from typing import Any, Generic, Hashable, Iterable, cast

from .descriptors import PluginDescriptor, PluginFactoryContext, PluginScope
from .registries import (
    ActivatedRegistry,
    CapabilityT,
    CapabilityBinding,
    CapabilityPolicy,
    CapabilitySchema,
    TypedRegistry,
)


_PLUGIN_ID_RE = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_SEMVER_RE = re.compile(
    r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
_CALLBACK_HOSTS: ContextVar[frozenset[int]] = ContextVar(
    "box_agent_plugin_callback_hosts",
    default=frozenset(),
)


def _is_valid_semver(version: str) -> bool:
    match = _SEMVER_RE.fullmatch(version)
    if match is None:
        return False
    prerelease = match.group(1)
    if prerelease is None:
        return True
    return all(
        not (identifier.isdigit() and len(identifier) > 1 and identifier[0] == "0")
        for identifier in prerelease.split(".")
    )


def _is_hashable(value: object) -> bool:
    try:
        hash(value)
    except Exception:
        return False
    return True


def _supports_dynamic_protocol(instance: object, port_type: type) -> bool:
    """Retain duck-typed proxies after Python 3.12 made Protocol checks static."""

    members = getattr(port_type, "__protocol_attrs__", ())
    if not members or not callable(getattr(type(instance), "__getattr__", None)):
        return False
    for name in members:
        try:
            # Python resolves special methods on the type, not via __getattr__.
            value = (
                inspect.getattr_static(type(instance), name)
                if name.startswith("__") and name.endswith("__")
                else getattr(instance, name)
            )
        except AttributeError:
            return False
        if callable(getattr(port_type, name, None)) and not callable(value):
            return False
    return True


class PluginError(RuntimeError):
    """Base error for plugin-host lifecycle failures."""


class PluginValidationError(PluginError):
    """Raised when static descriptors do not form a valid plugin set."""


class PluginDependencyCycleError(PluginValidationError):
    """Raised when plugin dependencies contain a cycle."""


class PluginScopeError(PluginError):
    """Raised when activation lacks scope-specific identity."""


class PluginCleanupError(PluginError):
    """Collect cleanup failures without stopping reverse-order disposal."""

    def __init__(self, errors: Iterable[BaseException]) -> None:
        self.errors = tuple(errors)
        details = "; ".join(str(error) for error in self.errors)
        super().__init__(f"plugin cleanup failed: {details}")


@dataclass(slots=True)
class _InstanceRecord:
    descriptor: PluginDescriptor
    instance: object
    disposed: bool = False


@dataclass(frozen=True, slots=True)
class PluginContribution(Generic[CapabilityT]):
    """把激活后的能力实例与真实插件描述符关联。"""

    descriptor: PluginDescriptor
    instance: CapabilityT


class PluginActivation:
    """One immutable registry plus the run-scoped resources that own it."""

    __slots__ = ("_disposed", "_host", "_run_records", "registry", "_contributions")

    def __init__(
        self,
        host: "PluginHost",
        registry: ActivatedRegistry,
        run_records: tuple[_InstanceRecord, ...],
        contributions: tuple[PluginContribution, ...] = (),
    ) -> None:
        self._host = host
        self.registry = registry
        self._run_records = run_records
        self._disposed = False
        self._contributions = contributions

    def contributions(self, port_type: type[CapabilityT]) -> tuple[PluginContribution[CapabilityT], ...]:
        """按激活顺序取得指定能力的实例与归属。"""
        return cast(tuple[PluginContribution[CapabilityT], ...], tuple(
            item for item in self._contributions if port_type in item.descriptor.capabilities
        ))

    async def dispose(self) -> None:
        """Dispose this activation's run-scoped instances once."""

        await self._host._dispose_activation(self)

    async def __aenter__(self) -> "PluginActivation":
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        await self.dispose()


class PluginHost:
    """Resolve an explicit, immutable descriptor collection without scanning."""

    def __init__(
        self,
        descriptors: Iterable[PluginDescriptor],
        *,
        schema: CapabilitySchema | None,
    ) -> None:
        self._descriptors = tuple(descriptors)
        self._schema = schema
        self._process_instances: dict[str, _InstanceRecord] = {}
        self._session_instances: dict[Hashable, dict[str, _InstanceRecord]] = {}
        self._closing_sessions: set[Hashable] = set()
        self._pending_run_rollbacks: dict[Hashable, list[_InstanceRecord]] = {}
        self._live_records: list[_InstanceRecord] = []
        self._closed = False
        self._lock = asyncio.Lock()
        self._operation_in_flight: asyncio.Future[None] | None = None

    def _ensure_not_callback_reentry(self, operation: str) -> None:
        if id(self) in _CALLBACK_HOSTS.get():
            raise PluginError(
                f"plugin callback re-entry is not allowed: {operation}"
            )

    async def _reserve_operation(self, operation: str) -> asyncio.Future[None]:
        """Serialize lifecycle operations without holding the state lock."""

        self._ensure_not_callback_reentry(operation)
        while True:
            async with self._lock:
                active = self._operation_in_flight
                if active is None:
                    reservation = asyncio.get_running_loop().create_future()
                    self._operation_in_flight = reservation
                    return reservation
            await asyncio.shield(active)

    async def _release_operation(
        self,
        reservation: asyncio.Future[None],
    ) -> None:
        async with self._lock:
            if self._operation_in_flight is reservation:
                self._operation_in_flight = None
                if not reservation.done():
                    reservation.set_result(None)

    async def _invoke_callback(self, callback: Any, *args: object) -> Any:
        callback_hosts = _CALLBACK_HOSTS.get()
        token = _CALLBACK_HOSTS.set(callback_hosts | {id(self)})
        try:
            result = callback(*args)
            if inspect.isawaitable(result):
                return await result
            return result
        finally:
            _CALLBACK_HOSTS.reset(token)

    def discover(self) -> tuple[PluginDescriptor, ...]:
        """Return only the descriptors explicitly supplied by the caller."""

        return self._descriptors

    @property
    def has_live_instances(self) -> bool:
        """Whether this Host still owns records that close must release."""
        return bool(self._live_records)

    def validate(self) -> None:
        """Validate the whole static graph before any factory is invoked."""

        self.resolve_dependencies()

    def _select_descriptors(
        self, plugin_ids: Iterable[str] | None,
    ) -> tuple[PluginDescriptor, ...]:
        """Validate catalog metadata, then cardinality for the selected closure."""

        policies = self._validate_schema(self._schema)
        descriptor_ids: dict[str, PluginDescriptor] = {}
        capability_ids: dict[type[Any], list[str]] = {
            port_type: [] for port_type in policies
        }
        for descriptor in self._descriptors:
            if not isinstance(descriptor, PluginDescriptor):
                raise PluginValidationError("malformed plugin descriptor")
            self._validate_descriptor(descriptor)
            if descriptor.plugin_id in descriptor_ids:
                raise PluginValidationError(
                    f"duplicate plugin id: {descriptor.plugin_id}"
                )
            descriptor_ids[descriptor.plugin_id] = descriptor
            for port_type in descriptor.capabilities:
                if port_type not in policies:
                    raise PluginValidationError(
                        "undeclared capability in descriptor "
                        f"{descriptor.plugin_id!r}: {port_type.__qualname__}"
                    )

        selected: set[str] = set()
        pending = list(descriptor_ids if plugin_ids is None else plugin_ids)
        while pending:
            plugin_id = pending.pop()
            if not isinstance(plugin_id, str) or plugin_id not in descriptor_ids:
                raise PluginValidationError(f"unknown plugin id: {plugin_id!r}")
            if plugin_id in selected:
                continue
            selected.add(plugin_id)
            descriptor = descriptor_ids[plugin_id]
            for dependency in descriptor.dependencies:
                if dependency not in descriptor_ids:
                    raise PluginValidationError(
                        f"missing dependency {dependency!r} for {descriptor.plugin_id!r}"
                    )
                pending.append(dependency)

        descriptors = tuple(d for d in self._descriptors if d.plugin_id in selected)
        lifetime = {PluginScope.PROCESS: 0, PluginScope.SESSION: 1, PluginScope.RUN: 2}
        for descriptor in descriptors:
            for dependency in descriptor.dependencies:
                if lifetime[descriptor.scope] < lifetime[descriptor_ids[dependency].scope]:
                    raise PluginValidationError(
                        f"plugin {descriptor.plugin_id!r} cannot depend on "
                        f"shorter-lived plugin {dependency!r}"
                    )
            for port_type in descriptor.capabilities:
                providers = capability_ids[port_type]
                if providers and policies[port_type] is not CapabilityPolicy.MULTI:
                    raise PluginValidationError(
                        "duplicate capability registration: "
                        f"{port_type.__qualname__} "
                        f"({providers[0]}, {descriptor.plugin_id})"
                    )
                providers.append(descriptor.plugin_id)

        for port_type, policy in policies.items():
            if (
                policy is CapabilityPolicy.REQUIRED_SINGLE
                and not capability_ids[port_type]
            ):
                raise PluginValidationError(
                    f"missing required capability: {port_type.__qualname__}"
                )
        return descriptors

    @staticmethod
    def _validate_schema(
        schema: CapabilitySchema | None,
    ) -> dict[type[Any], CapabilityPolicy]:
        if not isinstance(schema, CapabilitySchema):
            raise PluginValidationError("malformed capability schema")
        if not isinstance(schema.bindings, tuple):
            raise PluginValidationError(
                "malformed capability schema: bindings must be an immutable tuple"
            )

        validated: list[CapabilityBinding] = []
        for binding in schema.bindings:
            if not isinstance(binding, CapabilityBinding):
                raise PluginValidationError(
                    "malformed capability schema binding"
                )
            if not isinstance(binding.port_type, type):
                raise PluginValidationError(
                    "malformed capability schema Port type"
                )
            if not _is_hashable(binding.port_type):
                raise PluginValidationError(
                    "malformed capability schema: capability must be a "
                    "hashable Port type"
                )
            if not isinstance(binding.policy, CapabilityPolicy):
                raise PluginValidationError(
                    "malformed capability schema policy"
                )
            validated.append(binding)

        policies: dict[type[Any], CapabilityPolicy] = {}
        for binding in validated:
            if binding.port_type in policies:
                raise PluginValidationError(
                    "malformed capability schema: overlapping binding for "
                    f"{binding.port_type.__qualname__}"
                )
            policies[binding.port_type] = binding.policy
        return policies

    @staticmethod
    def _validate_descriptor(descriptor: PluginDescriptor) -> None:
        if not isinstance(descriptor.plugin_id, str) or not _PLUGIN_ID_RE.fullmatch(
            descriptor.plugin_id
        ):
            raise PluginValidationError(f"malformed plugin id: {descriptor.plugin_id!r}")
        if not isinstance(descriptor.version, str) or not _is_valid_semver(
            descriptor.version
        ):
            raise PluginValidationError(
                f"malformed version for {descriptor.plugin_id!r}: {descriptor.version!r}"
            )
        if not isinstance(descriptor.scope, PluginScope):
            raise PluginValidationError(
                f"malformed scope for {descriptor.plugin_id!r}: {descriptor.scope!r}"
            )
        if not isinstance(descriptor.dependencies, tuple):
            raise PluginValidationError("plugin dependencies must be an immutable tuple")
        for dependency in descriptor.dependencies:
            if not isinstance(dependency, str) or not _PLUGIN_ID_RE.fullmatch(dependency):
                raise PluginValidationError(f"malformed dependency id: {dependency!r}")
            if dependency == descriptor.plugin_id:
                raise PluginValidationError("plugin cannot depend on itself")
        if len(set(descriptor.dependencies)) != len(descriptor.dependencies):
            raise PluginValidationError("duplicate plugin dependencies")
        if not isinstance(descriptor.capabilities, tuple) or not descriptor.capabilities:
            raise PluginValidationError("plugin capabilities must be a non-empty tuple")
        for port_type in descriptor.capabilities:
            if not isinstance(port_type, type):
                raise PluginValidationError(
                    "plugin capabilities must contain Port types"
                )
            if not _is_hashable(port_type):
                raise PluginValidationError(
                    "plugin capabilities must contain hashable Port types"
                )
        if len(set(descriptor.capabilities)) != len(descriptor.capabilities):
            raise PluginValidationError("duplicate capability declaration")
        if (descriptor.factory is None) == (descriptor.context_factory is None):
            raise PluginValidationError("plugin requires exactly one factory or context_factory")
        if descriptor.factory is not None and not callable(descriptor.factory):
            raise PluginValidationError("plugin factory must be callable")
        if descriptor.context_factory is not None and not callable(descriptor.context_factory):
            raise PluginValidationError("plugin context_factory must be callable")
        if descriptor.disposer is not None and not callable(descriptor.disposer):
            raise PluginValidationError("plugin disposer must be callable")

    def resolve_dependencies(
        self, plugin_ids: Iterable[str] | None = None,
    ) -> tuple[PluginDescriptor, ...]:
        """Return a deterministic stable topological ordering."""

        descriptors = self._select_descriptors(plugin_ids)
        by_id = {descriptor.plugin_id: descriptor for descriptor in descriptors}
        indexes = {
            descriptor.plugin_id: index for index, descriptor in enumerate(descriptors)
        }
        indegrees = {
            descriptor.plugin_id: len(descriptor.dependencies)
            for descriptor in descriptors
        }
        dependents: dict[str, list[str]] = {
            descriptor.plugin_id: [] for descriptor in descriptors
        }
        for descriptor in descriptors:
            for dependency in descriptor.dependencies:
                dependents[dependency].append(descriptor.plugin_id)

        ready = [
            (indexes[plugin_id], plugin_id)
            for plugin_id, indegree in indegrees.items()
            if indegree == 0
        ]
        heapq.heapify(ready)
        ordered: list[PluginDescriptor] = []
        while ready:
            _, plugin_id = heapq.heappop(ready)
            ordered.append(by_id[plugin_id])
            for dependent_id in sorted(
                dependents[plugin_id], key=indexes.__getitem__
            ):
                indegrees[dependent_id] -= 1
                if indegrees[dependent_id] == 0:
                    heapq.heappush(ready, (indexes[dependent_id], dependent_id))

        if len(ordered) != len(descriptors):
            cyclic_ids = [
                plugin_id for plugin_id, indegree in indegrees.items() if indegree > 0
            ]
            raise PluginDependencyCycleError(
                f"plugin dependency cycle: {', '.join(cyclic_ids)}"
            )
        return tuple(ordered)

    async def activate(
        self,
        *,
        session_key: Hashable | None = None,
        plugin_ids: Iterable[str] | None = None,
        scopes: Iterable[PluginScope] | None = None,
        contexts: Mapping[PluginScope, object] | None = None,
    ) -> PluginActivation:
        """Activate or reuse instances and return an immutable registry."""

        plan = self.resolve_dependencies(plugin_ids)
        selected_scopes = tuple(PluginScope) if scopes is None else tuple(scopes)
        if any(not isinstance(scope, PluginScope) for scope in selected_scopes):
            raise PluginScopeError("activation scopes must contain PluginScope values")
        if contexts is not None and (
            not isinstance(contexts, Mapping)
            or any(not isinstance(scope, PluginScope) for scope in contexts)
        ):
            raise PluginScopeError("contexts must map PluginScope values to context objects")
        scope_contexts = {} if contexts is None else dict(contexts)
        ordered = tuple(d for d in plan if d.scope in selected_scopes)
        if any(
            descriptor.scope is PluginScope.SESSION for descriptor in ordered
        ):
            if session_key is None:
                raise PluginScopeError("session-scoped plugins require a session key")
        if session_key is not None:
            try:
                hash(session_key)
            except TypeError as error:
                raise PluginScopeError("session key must be hashable") from error

        reservation = await self._reserve_operation("activate")
        try:
            async with self._lock:
                if self._closed:
                    raise PluginError("plugin host is closed")
                if session_key in self._closing_sessions:
                    raise PluginScopeError(
                        "session cleanup is incomplete; retry dispose_session first"
                    )
            if self._schema is None:  # Guarded by resolve_dependencies().
                raise PluginValidationError("malformed capability schema")
            builder = TypedRegistry(self._schema)
            instances: dict[str, object] = {}
            # Resolve excluded dependencies before invoking any factory. Partial
            # activation may consume already-owned process/session dependencies.
            active_ids = {descriptor.plugin_id for descriptor in ordered}
            by_id = {descriptor.plugin_id: descriptor for descriptor in plan}
            for descriptor in ordered:
                for dependency in descriptor.dependencies:
                    if dependency in active_ids:
                        continue
                    dependency_descriptor = by_id[dependency]
                    cache = (
                        self._process_instances
                        if dependency_descriptor.scope is PluginScope.PROCESS
                        else self._session_instances.get(session_key, {})
                        if dependency_descriptor.scope is PluginScope.SESSION
                        else {}
                    )
                    record = cache.get(dependency)
                    if record is None:
                        raise PluginScopeError(
                            f"dependency {dependency!r} is outside activation scopes "
                            "and has no cached instance"
                        )
                    instances[dependency] = record.instance
            created: list[_InstanceRecord] = []
            run_records: list[_InstanceRecord] = []
            contributions: list[PluginContribution] = []
            try:
                for descriptor in ordered:
                    record = await self._get_or_create(
                        descriptor,
                        session_key=session_key,
                        created=created,
                        factory_context=PluginFactoryContext(
                            scope_contexts.get(descriptor.scope),
                            {dependency: instances[dependency] for dependency in descriptor.dependencies},
                        ),
                    )
                    instances[descriptor.plugin_id] = record.instance
                    contributions.append(PluginContribution(descriptor, record.instance))
                    if descriptor.scope is PluginScope.RUN:
                        run_records.append(record)
                    for port_type in descriptor.capabilities:
                        builder.register(port_type, record.instance)
            except BaseException as activation_error:
                async with self._lock:
                    self._remove_cached_records(created, session_key=session_key)
                cleanup_errors, cleanup_cancellation = await self._dispose_records(
                    reversed(created)
                )
                async with self._lock:
                    self._remove_live_records(
                        record for record in created if record.disposed
                    )
                    self._retain_run_rollback(created, session_key=session_key)
                self._raise_activation_failure(
                    activation_error,
                    cleanup_errors=cleanup_errors,
                    cleanup_cancellation=cleanup_cancellation,
                )
                raise AssertionError("unreachable")

            return PluginActivation(self, builder.freeze(), tuple(run_records), tuple(contributions))
        finally:
            await self._release_operation(reservation)

    async def _get_or_create(
        self,
        descriptor: PluginDescriptor,
        *,
        session_key: Hashable | None,
        created: list[_InstanceRecord],
        factory_context: PluginFactoryContext,
    ) -> _InstanceRecord:
        async with self._lock:
            if descriptor.scope is PluginScope.PROCESS:
                cached = self._process_instances.get(descriptor.plugin_id)
                if cached is not None:
                    return cached
            elif descriptor.scope is PluginScope.SESSION:
                session_cache = self._session_instances.setdefault(session_key, {})
                cached = session_cache.get(descriptor.plugin_id)
                if cached is not None:
                    return cached

        if descriptor.context_factory is not None:
            instance = await self._invoke_callback(descriptor.context_factory, factory_context)
        else:
            instance = await self._invoke_callback(descriptor.factory)
        record = _InstanceRecord(descriptor=descriptor, instance=instance)
        try:
            self._validate_runtime_ports(descriptor, instance)
        except BaseException as validation_error:
            async with self._lock:
                self._live_records.append(record)
            cleanup_errors, cleanup_cancellation = await self._dispose_records((record,))
            async with self._lock:
                if record.disposed:
                    self._remove_live_records((record,))
                self._retain_run_rollback((record,), session_key=session_key)
            if cleanup_cancellation is not None:
                if cleanup_errors:
                    raise cleanup_cancellation from PluginCleanupError(cleanup_errors)
                raise cleanup_cancellation from validation_error
            if cleanup_errors:
                raise validation_error from PluginCleanupError(cleanup_errors)
            raise

        async with self._lock:
            created.append(record)
            self._live_records.append(record)
            if descriptor.scope is PluginScope.PROCESS:
                self._process_instances[descriptor.plugin_id] = record
            elif descriptor.scope is PluginScope.SESSION:
                self._session_instances[session_key][descriptor.plugin_id] = record
        return record

    @staticmethod
    def _validate_runtime_ports(
        descriptor: PluginDescriptor,
        instance: object,
    ) -> None:
        for port_type in descriptor.capabilities:
            if not (
                getattr(port_type, "_is_protocol", False)
                and getattr(port_type, "_is_runtime_protocol", False)
            ):
                continue
            if not (
                isinstance(instance, port_type)
                or _supports_dynamic_protocol(instance, port_type)
            ):
                raise PluginValidationError(
                    f"plugin {descriptor.plugin_id!r} factory result does not "
                    f"satisfy runtime Port {port_type.__qualname__}"
                )

    @staticmethod
    def _raise_activation_failure(
        activation_error: BaseException,
        *,
        cleanup_errors: tuple[BaseException, ...],
        cleanup_cancellation: asyncio.CancelledError | None,
    ) -> None:
        cleanup_failure = (
            PluginCleanupError(cleanup_errors) if cleanup_errors else None
        )
        if isinstance(activation_error, asyncio.CancelledError):
            if cleanup_failure is not None:
                raise activation_error from cleanup_failure
            raise activation_error
        if cleanup_cancellation is not None:
            if cleanup_failure is not None:
                raise cleanup_cancellation from cleanup_failure
            raise cleanup_cancellation from activation_error
        if cleanup_failure is not None:
            raise activation_error from cleanup_failure
        raise activation_error

    def _remove_cached_records(
        self,
        records: Iterable[_InstanceRecord],
        *,
        session_key: Hashable | None,
    ) -> None:
        for record in records:
            descriptor = record.descriptor
            if descriptor.scope is PluginScope.PROCESS:
                if self._process_instances.get(descriptor.plugin_id) is record:
                    del self._process_instances[descriptor.plugin_id]
            elif descriptor.scope is PluginScope.SESSION:
                session_cache = self._session_instances.get(session_key)
                if (
                    session_cache is not None
                    and session_cache.get(descriptor.plugin_id) is record
                ):
                    del session_cache[descriptor.plugin_id]
                    if not session_cache:
                        del self._session_instances[session_key]

    async def _dispose_activation(self, activation: PluginActivation) -> None:
        reservation = await self._reserve_operation("dispose activation")
        try:
            if activation._disposed:
                return
            records = activation._run_records
            cleanup_errors, cancellation = await self._dispose_records(
                reversed(records)
            )
            async with self._lock:
                completed = tuple(record for record in records if record.disposed)
                self._remove_live_records(completed)
                activation._run_records = tuple(
                    record for record in records if not record.disposed
                )
                activation._disposed = not activation._run_records
            self._raise_cleanup_failures(cleanup_errors, cancellation)
        finally:
            await self._release_operation(reservation)

    def _retain_run_rollback(
        self,
        records: Iterable[_InstanceRecord],
        *,
        session_key: Hashable | None,
    ) -> None:
        """Keep failed activation cleanup owned by its session after activate raises."""
        if session_key is None:
            return
        retained = [
            record for record in records
            if record.descriptor.scope is PluginScope.RUN and not record.disposed
        ]
        if retained:
            pending = self._pending_run_rollbacks.get(session_key, ())
            known = {id(record) for record in (*pending, *retained)}
            # Validation rollback reaches here before its dependencies' outer
            # rollback. Preserve creation order, not exception arrival order.
            self._pending_run_rollbacks[session_key] = [
                record for record in self._live_records if id(record) in known
            ]
            self._closing_sessions.add(session_key)

    async def dispose_session(self, session_key: Hashable) -> None:
        """Retry this session's failed RUN rollback before releasing SESSION resources."""

        reservation = await self._reserve_operation("dispose session")
        try:
            pending = tuple(self._pending_run_rollbacks.get(session_key, ()))
            rollback_errors, rollback_cancellation = await self._dispose_records(
                reversed(pending)
            )
            async with self._lock:
                remaining = [record for record in pending if not record.disposed]
                if remaining:
                    self._pending_run_rollbacks[session_key] = remaining
                else:
                    self._pending_run_rollbacks.pop(session_key, None)
                self._remove_live_records(record for record in pending if record.disposed)
            if rollback_cancellation is not None:
                self._raise_cleanup_failures(rollback_errors, rollback_cancellation)
            async with self._lock:
                owned_records = tuple(
                    self._session_instances.get(session_key, {}).values()
                )
                if owned_records:
                    self._closing_sessions.add(session_key)
            cleanup_errors, cancellation = await self._dispose_records(
                reversed(owned_records)
            )
            async with self._lock:
                session_cache = self._session_instances.get(session_key)
                if session_cache is not None:
                    for record in owned_records:
                        if (
                            record.disposed
                            and session_cache.get(record.descriptor.plugin_id) is record
                        ):
                            del session_cache[record.descriptor.plugin_id]
                    if not session_cache:
                        del self._session_instances[session_key]
                if session_key not in self._session_instances:
                    self._closing_sessions.discard(session_key)
                self._remove_live_records(
                    record for record in owned_records if record.disposed
                )
            self._raise_cleanup_failures(rollback_errors + cleanup_errors, cancellation)
        finally:
            await self._release_operation(reservation)

    async def close(self) -> None:
        """Dispose every remaining instance once, in reverse creation order."""

        reservation = await self._reserve_operation("close")
        try:
            async with self._lock:
                if self._closed and not self._live_records:
                    return
                self._closed = True
                records = tuple(self._live_records)
            cleanup_errors, cancellation = await self._dispose_records(
                reversed(records)
            )
            async with self._lock:
                completed = tuple(record for record in records if record.disposed)
                self._remove_records_from_all_caches(completed)
                self._remove_live_records(completed)
            self._raise_cleanup_failures(cleanup_errors, cancellation)
        finally:
            await self._release_operation(reservation)

    async def _dispose_records(
        self,
        records: Iterable[_InstanceRecord],
    ) -> tuple[tuple[BaseException, ...], asyncio.CancelledError | None]:
        errors: list[BaseException] = []
        cancellation: asyncio.CancelledError | None = None
        for record in records:
            if record.disposed:
                continue
            disposer = record.descriptor.disposer
            if disposer is None:
                record.disposed = True
                continue
            try:
                await self._invoke_callback(disposer, record.instance)
            except asyncio.CancelledError as error:
                if cancellation is None:
                    cancellation = error
            except BaseException as error:
                record.disposed = True
                errors.append(error)
            else:
                record.disposed = True
        return tuple(errors), cancellation

    @staticmethod
    def _raise_cleanup_failures(
        errors: tuple[BaseException, ...],
        cancellation: asyncio.CancelledError | None,
    ) -> None:
        if cancellation is not None:
            if errors:
                raise cancellation from PluginCleanupError(errors)
            raise cancellation
        if errors:
            raise PluginCleanupError(errors)

    def _remove_records_from_all_caches(
        self,
        records: Iterable[_InstanceRecord],
    ) -> None:
        record_ids = {id(record) for record in records}
        if not record_ids:
            return
        for session_key, pending in tuple(self._pending_run_rollbacks.items()):
            remaining = [record for record in pending if id(record) not in record_ids]
            if remaining:
                self._pending_run_rollbacks[session_key] = remaining
            else:
                del self._pending_run_rollbacks[session_key]
        self._process_instances = {
            plugin_id: record
            for plugin_id, record in self._process_instances.items()
            if id(record) not in record_ids
        }
        empty_sessions: list[Hashable] = []
        for session_key, session_cache in self._session_instances.items():
            self._session_instances[session_key] = {
                plugin_id: record
                for plugin_id, record in session_cache.items()
                if id(record) not in record_ids
            }
            if not self._session_instances[session_key]:
                empty_sessions.append(session_key)
        for session_key in empty_sessions:
            del self._session_instances[session_key]
            self._closing_sessions.discard(session_key)

    def _remove_live_records(self, records: Iterable[_InstanceRecord]) -> None:
        record_ids = {id(record) for record in records}
        if not record_ids:
            return
        self._live_records[:] = [
            record for record in self._live_records if id(record) not in record_ids
        ]


__all__ = [
    "PluginActivation",
    "PluginCleanupError",
    "PluginDependencyCycleError",
    "PluginError",
    "PluginHost",
    "PluginScopeError",
    "PluginValidationError",
]

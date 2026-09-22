"""租约存储层：封装 Redis 连接与 Lua 脚本调用。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Optional

from redis.asyncio import Redis

from . import lua
from .config import Settings

# 续租失败原因码（与 lua.RENEW_SCRIPT 返回值对应）
RENEW_REASONS = {
    1: "lease_not_found",
    2: "token_mismatch",
    3: "fencing_mismatch",
    4: "lease_expired",
}


@dataclass
class AcquireResult:
    acquired: bool
    token: Optional[str]
    fencing_token: int
    expire_at_ms: int
    server_time_ms: int


@dataclass
class RenewResult:
    renewed: bool
    reason: Optional[str]
    expire_at_ms: int
    server_time_ms: int


@dataclass
class ValidateResult:
    valid: bool
    current_fencing_token: int
    expire_at_ms: int
    server_time_ms: int


class LeaseStore:
    """对 Redis 租约状态机的原子操作封装。"""

    def __init__(self, redis: Redis, settings: Settings):
        self._redis = redis
        self._prefix = settings.key_prefix
        self._acquire = redis.register_script(lua.ACQUIRE_SCRIPT)
        self._renew = redis.register_script(lua.RENEW_SCRIPT)
        self._release = redis.register_script(lua.RELEASE_SCRIPT)
        self._validate = redis.register_script(lua.VALIDATE_SCRIPT)
        self._status = redis.register_script(lua.STATUS_SCRIPT)

    def _lease_key(self, task_key: str) -> str:
        return f"{self._prefix}lease:{task_key}"

    def _fencing_key(self, task_key: str) -> str:
        return f"{self._prefix}fencing:{task_key}"

    async def acquire(self, task_key: str, ttl_ms: int) -> AcquireResult:
        token = uuid.uuid4().hex
        acquired, fencing, expire_at, now = await self._acquire(
            keys=[self._lease_key(task_key), self._fencing_key(task_key)],
            args=[ttl_ms, token],
        )
        return AcquireResult(
            acquired=bool(acquired),
            token=token if acquired else None,
            fencing_token=int(fencing),
            expire_at_ms=int(expire_at),
            server_time_ms=int(now),
        )

    async def renew(self, task_key: str, token: str, fencing_token: int, ttl_ms: int) -> RenewResult:
        ok, code_or_expire, now = await self._renew(
            keys=[self._lease_key(task_key)],
            args=[token, fencing_token, ttl_ms],
        )
        if ok:
            return RenewResult(True, None, int(code_or_expire), int(now))
        return RenewResult(False, RENEW_REASONS.get(int(code_or_expire), "unknown"), 0, int(now))

    async def release(self, task_key: str, token: str, fencing_token: int) -> bool:
        released = await self._release(
            keys=[self._lease_key(task_key)],
            args=[token, fencing_token],
        )
        return bool(released)

    async def validate(self, task_key: str, token: str, fencing_token: int) -> ValidateResult:
        valid, fencing, expire_at, now = await self._validate(
            keys=[self._lease_key(task_key)],
            args=[token, fencing_token],
        )
        return ValidateResult(bool(valid), int(fencing), int(expire_at), int(now))

    async def status(self, task_key: str) -> tuple[dict, int]:
        """返回 (租约字段 dict, 服务器时间 ms)。租约不存在时 dict 为空。"""
        data, now = await self._status(keys=[self._lease_key(task_key)])
        fields = {data[i]: data[i + 1] for i in range(0, len(data), 2)}
        return fields, int(now)

    async def ping(self) -> bool:
        return bool(await self._redis.ping())

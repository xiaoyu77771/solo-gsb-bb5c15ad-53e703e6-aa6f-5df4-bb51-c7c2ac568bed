"""分布式租约协调 API。

多工作进程通过本服务按任务键协调独占执行权：
  - 申请（acquire）：任务空闲或旧租约已过期时签发 {token, fencing_token}
  - 续租（renew）/ 释放（release）：必须同时校验 token 与 fencing_token
  - 校验（validate）：下游据此判断一次操作是否仍属于当前租约
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from redis.asyncio import Redis

from .config import Settings, get_settings
from .schemas import (
    AcquireRequest,
    AcquireResponse,
    LeaseStatusResponse,
    ReleaseRequest,
    ReleaseResponse,
    RenewRequest,
    RenewResponse,
    ValidateRequest,
    ValidateResponse,
)
from .store import LeaseStore

router = APIRouter()


def get_store(request: Request) -> LeaseStore:
    return request.app.state.store


def get_app_settings(request: Request) -> Settings:
    return request.app.state.settings


def _resolve_ttl(ttl_ms: int | None, settings: Settings) -> int:
    ttl = ttl_ms if ttl_ms is not None else settings.default_ttl_ms
    if not settings.min_ttl_ms <= ttl <= settings.max_ttl_ms:
        raise HTTPException(
            status_code=422,
            detail=f"ttl_ms 必须在 [{settings.min_ttl_ms}, {settings.max_ttl_ms}] 毫秒内",
        )
    return ttl


@router.get("/healthz")
async def healthz(store: LeaseStore = Depends(get_store)):
    try:
        await store.ping()
    except Exception as exc:  # noqa: BLE001 - 健康检查需如实上报
        raise HTTPException(status_code=503, detail=f"redis unavailable: {exc}")
    return {"status": "ok"}


@router.post(
    "/v1/leases/acquire",
    response_model=AcquireResponse,
    responses={409: {"description": "任务已被其他持有者占用"}},
)
async def acquire(
    body: AcquireRequest,
    store: LeaseStore = Depends(get_store),
    settings: Settings = Depends(get_app_settings),
):
    """申请租约。仅当任务未被占用或旧租约已过期时成功。"""
    ttl = _resolve_ttl(body.ttl_ms, settings)
    result = await store.acquire(body.task_key, ttl)
    if not result.acquired:
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "lease_held",
                "current_fencing_token": result.fencing_token,
                "expire_at_ms": result.expire_at_ms,
                "server_time_ms": result.server_time_ms,
            },
        )
    return AcquireResponse(
        acquired=True,
        task_key=body.task_key,
        token=result.token,
        fencing_token=result.fencing_token,
        expire_at_ms=result.expire_at_ms,
        ttl_ms=ttl,
        server_time_ms=result.server_time_ms,
    )


@router.post(
    "/v1/leases/renew",
    response_model=RenewResponse,
    responses={409: {"description": "续租被拒绝（令牌/防栅序号不匹配或租约已过期）"}},
)
async def renew(
    body: RenewRequest,
    store: LeaseStore = Depends(get_store),
    settings: Settings = Depends(get_app_settings),
):
    """续租。必须同时匹配 token 与 fencing_token，且租约尚未过期。"""
    ttl = _resolve_ttl(body.ttl_ms, settings)
    result = await store.renew(body.task_key, body.token, body.fencing_token, ttl)
    if not result.renewed:
        raise HTTPException(
            status_code=409,
            detail={"reason": result.reason, "server_time_ms": result.server_time_ms},
        )
    return RenewResponse(
        renewed=True,
        task_key=body.task_key,
        expire_at_ms=result.expire_at_ms,
        ttl_ms=ttl,
        server_time_ms=result.server_time_ms,
    )


@router.post(
    "/v1/leases/release",
    response_model=ReleaseResponse,
    responses={409: {"description": "释放被拒绝（令牌/防栅序号不匹配）"}},
)
async def release(body: ReleaseRequest, store: LeaseStore = Depends(get_store)):
    """释放租约。旧持有者（令牌或防栅序号不符）无法影响当前租约。"""
    released = await store.release(body.task_key, body.token, body.fencing_token)
    if not released:
        raise HTTPException(status_code=409, detail={"reason": "token_or_fencing_mismatch"})
    return ReleaseResponse(released=True, task_key=body.task_key)


@router.post("/v1/leases/validate", response_model=ValidateResponse)
async def validate(body: ValidateRequest, store: LeaseStore = Depends(get_store)):
    """校验 (token, fencing_token) 是否仍为该任务的当前有效租约。

    下游在执行被保护的操作前调用本接口；valid=false 时必须放弃该操作。
    """
    result = await store.validate(body.task_key, body.token, body.fencing_token)
    if result.valid:
        reason = "ok"
    elif result.current_fencing_token == 0:
        reason = "lease_not_found"
    elif result.expire_at_ms <= result.server_time_ms:
        reason = "lease_expired"
    else:
        reason = "token_or_fencing_mismatch"
    return ValidateResponse(
        valid=result.valid,
        task_key=body.task_key,
        reason=reason,
        current_fencing_token=result.current_fencing_token,
        expire_at_ms=result.expire_at_ms,
        server_time_ms=result.server_time_ms,
    )


@router.get("/v1/leases/{task_key}", response_model=LeaseStatusResponse)
async def lease_status(task_key: str, store: LeaseStore = Depends(get_store)):
    """查询任务当前租约状态（不暴露 token）。"""
    fields, now = await store.status(task_key)
    expire_at = int(fields.get("expire_at", 0))
    fencing = int(fields.get("fencing", 0))
    held = bool(fields) and expire_at > now
    return LeaseStatusResponse(
        task_key=task_key,
        held=held,
        expired=bool(fields) and expire_at <= now,
        fencing_token=fencing,
        expire_at_ms=expire_at,
        remaining_ms=max(0, expire_at - now),
        server_time_ms=now,
    )


def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings = get_settings()
        redis = Redis.from_url(settings.redis_url, decode_responses=True)
        app.state.settings = settings
        app.state.store = LeaseStore(redis, settings)
        try:
            yield
        finally:
            await redis.aclose()

    application = FastAPI(
        title="Lease Coordination API",
        version="1.0.0",
        description="基于 Redis 的分布式租约（fencing token）协调服务",
        lifespan=lifespan,
    )
    application.include_router(router)
    return application


app = create_app()

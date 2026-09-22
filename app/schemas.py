"""请求 / 响应模型。"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class AcquireRequest(BaseModel):
    task_key: str = Field(min_length=1, max_length=256, description="任务键")
    ttl_ms: Optional[int] = Field(default=None, description="租约 TTL（毫秒），缺省用服务端默认值")


class AcquireResponse(BaseModel):
    acquired: bool
    task_key: str
    token: Optional[str] = None
    fencing_token: int
    expire_at_ms: int
    ttl_ms: Optional[int] = None
    server_time_ms: int


class RenewRequest(BaseModel):
    task_key: str = Field(min_length=1, max_length=256)
    token: str = Field(min_length=1)
    fencing_token: int
    ttl_ms: Optional[int] = None


class RenewResponse(BaseModel):
    renewed: bool
    task_key: str
    expire_at_ms: Optional[int] = None
    ttl_ms: Optional[int] = None
    server_time_ms: int


class ReleaseRequest(BaseModel):
    task_key: str = Field(min_length=1, max_length=256)
    token: str = Field(min_length=1)
    fencing_token: int


class ReleaseResponse(BaseModel):
    released: bool
    task_key: str


class ValidateRequest(BaseModel):
    task_key: str = Field(min_length=1, max_length=256)
    token: str = Field(min_length=1)
    fencing_token: int


class ValidateResponse(BaseModel):
    valid: bool
    task_key: str
    reason: str
    current_fencing_token: int
    expire_at_ms: int
    server_time_ms: int


class LeaseStatusResponse(BaseModel):
    task_key: str
    held: bool
    expired: bool
    fencing_token: int
    expire_at_ms: int
    remaining_ms: int
    server_time_ms: int


class ErrorResponse(BaseModel):
    detail: str
    reason: Optional[str] = None

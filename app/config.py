"""运行时配置。

所有配置项均可通过环境变量覆盖，前缀为 ``LEASE_``，例如：
``LEASE_REDIS_URL=redis://redis:6379/0``。
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LEASE_")

    # Redis 连接串
    redis_url: str = "redis://localhost:6379/0"

    # API 监听端口（供容器启动命令使用）
    port: int = 8000

    # Redis key 前缀，便于多环境共用一个实例
    key_prefix: str = "lease:"

    # TTL 约束（毫秒）
    default_ttl_ms: int = 30_000
    min_ttl_ms: int = 1_000
    max_ttl_ms: int = 600_000


@lru_cache
def get_settings() -> Settings:
    return Settings()

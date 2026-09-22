"""集成测试：需要可访问的 Redis（默认 redis://127.0.0.1:6379/15）。

运行方式：
    LEASE_TEST_REDIS_URL=redis://127.0.0.1:6379/15 pytest tests/ -v
"""

from __future__ import annotations

import os
import time
import uuid

import pytest
import redis as redis_lib
from fastapi.testclient import TestClient

REDIS_URL = os.environ.get("LEASE_TEST_REDIS_URL", "redis://127.0.0.1:6379/15")


def _redis_available() -> bool:
    try:
        client = redis_lib.Redis.from_url(REDIS_URL, socket_connect_timeout=1)
        client.ping()
        client.close()
        return True
    except redis_lib.RedisError:
        return False


pytestmark = pytest.mark.skipif(not _redis_available(), reason="Redis 不可达，跳过集成测试")


@pytest.fixture()
def client():
    os.environ["LEASE_REDIS_URL"] = REDIS_URL
    os.environ["LEASE_MIN_TTL_MS"] = "100"  # 便于测试短 TTL 过期
    from app.config import get_settings

    get_settings.cache_clear()
    from app.main import create_app

    with TestClient(create_app()) as test_client:
        yield test_client
    get_settings.cache_clear()


@pytest.fixture()
def task_key():
    return f"test-{uuid.uuid4().hex[:12]}"


def _acquire(client, task_key, ttl_ms=1000):
    return client.post("/v1/leases/acquire", json={"task_key": task_key, "ttl_ms": ttl_ms})


class TestAcquire:
    def test_acquire_free_task(self, client, task_key):
        resp = _acquire(client, task_key)
        assert resp.status_code == 200
        body = resp.json()
        assert body["acquired"] is True
        assert body["token"]
        assert body["fencing_token"] == 1
        assert body["expire_at_ms"] > body["server_time_ms"]

    def test_second_acquire_conflicts(self, client, task_key):
        assert _acquire(client, task_key).status_code == 200
        resp = _acquire(client, task_key)
        assert resp.status_code == 409
        assert resp.json()["detail"]["reason"] == "lease_held"

    def test_acquire_after_expiry_gets_higher_fencing(self, client, task_key):
        first = _acquire(client, task_key, ttl_ms=150).json()
        time.sleep(0.3)
        second = _acquire(client, task_key, ttl_ms=150).json()
        assert second["acquired"] is True
        assert second["fencing_token"] > first["fencing_token"]
        assert second["token"] != first["token"]

    def test_fencing_monotonic_across_leases(self, client, task_key):
        lease = _acquire(client, task_key).json()
        f1 = lease["fencing_token"]
        client.post(
            "/v1/leases/release",
            json={"task_key": task_key, "token": lease["token"], "fencing_token": lease["fencing_token"]},
        )
        f2 = _acquire(client, task_key).json()["fencing_token"]
        assert f1 == 1
        assert f2 == 2

    def test_ttl_out_of_range_rejected(self, client, task_key):
        resp = client.post("/v1/leases/acquire", json={"task_key": task_key, "ttl_ms": 10})
        assert resp.status_code == 422


class TestRenew:
    def test_renew_extends_lease(self, client, task_key):
        lease = _acquire(client, task_key, ttl_ms=300).json()
        time.sleep(0.15)
        resp = client.post(
            "/v1/leases/renew",
            json={"task_key": task_key, "token": lease["token"],
                  "fencing_token": lease["fencing_token"], "ttl_ms": 1000},
        )
        assert resp.status_code == 200
        assert resp.json()["expire_at_ms"] > lease["expire_at_ms"]

    def test_renew_with_wrong_token_rejected(self, client, task_key):
        lease = _acquire(client, task_key).json()
        resp = client.post(
            "/v1/leases/renew",
            json={"task_key": task_key, "token": "forged", "fencing_token": lease["fencing_token"]},
        )
        assert resp.status_code == 409
        assert resp.json()["detail"]["reason"] == "token_mismatch"

    def test_renew_with_wrong_fencing_rejected(self, client, task_key):
        lease = _acquire(client, task_key).json()
        resp = client.post(
            "/v1/leases/renew",
            json={"task_key": task_key, "token": lease["token"], "fencing_token": 999},
        )
        assert resp.status_code == 409
        assert resp.json()["detail"]["reason"] == "fencing_mismatch"

    def test_renew_after_expiry_rejected(self, client, task_key):
        lease = _acquire(client, task_key, ttl_ms=150).json()
        time.sleep(0.3)
        resp = client.post(
            "/v1/leases/renew",
            json={"task_key": task_key, "token": lease["token"], "fencing_token": lease["fencing_token"]},
        )
        assert resp.status_code == 409
        assert resp.json()["detail"]["reason"] == "lease_expired"


class TestRelease:
    def test_release_by_holder(self, client, task_key):
        lease = _acquire(client, task_key).json()
        resp = client.post(
            "/v1/leases/release",
            json={"task_key": task_key, "token": lease["token"], "fencing_token": lease["fencing_token"]},
        )
        assert resp.status_code == 200
        assert resp.json()["released"] is True
        # 释放后可立即被重新申请
        assert _acquire(client, task_key).status_code == 200

    def test_release_with_wrong_fencing_rejected(self, client, task_key):
        lease = _acquire(client, task_key).json()
        resp = client.post(
            "/v1/leases/release",
            json={"task_key": task_key, "token": lease["token"], "fencing_token": lease["fencing_token"] + 1},
        )
        assert resp.status_code == 409

    def test_stale_holder_cannot_touch_new_lease(self, client, task_key):
        """旧持有者超时后，其释放/续租不得影响新租约。"""
        old = _acquire(client, task_key, ttl_ms=150).json()
        time.sleep(0.3)
        new = _acquire(client, task_key).json()

        # 旧持有者尝试释放新租约 -> 拒绝
        resp = client.post(
            "/v1/leases/release",
            json={"task_key": task_key, "token": old["token"], "fencing_token": old["fencing_token"]},
        )
        assert resp.status_code == 409

        # 旧持有者尝试续租 -> 拒绝
        resp = client.post(
            "/v1/leases/renew",
            json={"task_key": task_key, "token": old["token"], "fencing_token": old["fencing_token"]},
        )
        assert resp.status_code == 409

        # 新租约依然有效
        resp = client.post(
            "/v1/leases/validate",
            json={"task_key": task_key, "token": new["token"], "fencing_token": new["fencing_token"]},
        )
        assert resp.json()["valid"] is True


class TestValidate:
    def test_validate_current_holder(self, client, task_key):
        lease = _acquire(client, task_key).json()
        resp = client.post(
            "/v1/leases/validate",
            json={"task_key": task_key, "token": lease["token"], "fencing_token": lease["fencing_token"]},
        )
        body = resp.json()
        assert body["valid"] is True
        assert body["reason"] == "ok"

    def test_validate_after_expiry(self, client, task_key):
        lease = _acquire(client, task_key, ttl_ms=150).json()
        time.sleep(0.3)
        resp = client.post(
            "/v1/leases/validate",
            json={"task_key": task_key, "token": lease["token"], "fencing_token": lease["fencing_token"]},
        )
        body = resp.json()
        assert body["valid"] is False
        assert body["reason"] == "lease_expired"

    def test_validate_unknown_task(self, client, task_key):
        resp = client.post(
            "/v1/leases/validate",
            json={"task_key": task_key, "token": "x", "fencing_token": 1},
        )
        body = resp.json()
        assert body["valid"] is False
        assert body["reason"] == "lease_not_found"

    def test_validate_stale_fencing(self, client, task_key):
        old = _acquire(client, task_key, ttl_ms=150).json()
        time.sleep(0.3)
        _acquire(client, task_key)
        resp = client.post(
            "/v1/leases/validate",
            json={"task_key": task_key, "token": old["token"], "fencing_token": old["fencing_token"]},
        )
        assert resp.json()["valid"] is False


class TestStatus:
    def test_status_of_held_and_free_task(self, client, task_key):
        resp = client.get(f"/v1/leases/{task_key}")
        assert resp.json()["held"] is False

        _acquire(client, task_key, ttl_ms=1000)
        body = client.get(f"/v1/leases/{task_key}").json()
        assert body["held"] is True
        assert body["fencing_token"] == 1
        assert 0 < body["remaining_ms"] <= 1000
        assert "token" not in body  # 状态接口不暴露 token

    def test_healthz(self, client):
        assert client.get("/healthz").json() == {"status": "ok"}

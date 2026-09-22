# 分布式租约协调 API（Lease Coordination API）

供多个工作进程协调**独占任务**执行权的租约服务。基于 **Python + FastAPI + Redis**，
所有状态转换在 Redis 内以 **Lua 脚本原子完成**，过期判断统一使用 **Redis 服务器时间**
（`TIME` 命令），不依赖任何客户端时钟。

## 核心概念

| 概念 | 说明 |
| --- | --- |
| `task_key` | 任务键，同一时刻最多一个持有者 |
| `token` | 所有权令牌（UUID），每次签发重新生成，标识"哪一把"租约 |
| `fencing_token` | 防栅序号，每个任务键单调递增（1, 2, 3, …），标识"第几代"租约 |
| `ttl_ms` | 租约有效期（毫秒），到期未续租即自动失效 |

**为什么需要防栅序号**：持有者可能因 GC 停顿、网络分区而"假死"，其租约超时后被他人接管。
当它恢复时，单凭 token 已无法通过校验（token 不匹配）；下游资源还可以用 fencing token
做最后一道防线——拒绝一切序号小于已见最大值的写操作，从而彻底隔绝旧持有者。

**旧持有者隔离**：续租与释放必须同时匹配 `token` + `fencing_token`。旧持有者超时后，
其任何请求都无法影响新租约（见 `tests/test_leases.py::test_stale_holder_cannot_touch_new_lease`）。

## 快速开始（一条命令）

```bash
docker compose up --build
```

该命令同时启动 Redis 与 API 服务（API 等待 Redis 健康检查后启动）。

- **API 地址**：http://localhost:8000
- **交互式文档（Swagger UI）**：http://localhost:8000/docs
- **Redis**：localhost:6379

验证：

```bash
curl http://localhost:8000/healthz
# {"status":"ok"}
```

## 配置

全部通过环境变量配置（前缀 `LEASE_`）：

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `LEASE_REDIS_URL` | `redis://localhost:6379/0` | Redis 连接串 |
| `LEASE_PORT` | `8000` | API 监听端口（容器启动命令使用） |
| `LEASE_KEY_PREFIX` | `lease:` | Redis key 前缀 |
| `LEASE_DEFAULT_TTL_MS` | `30000` | 默认租约 TTL（毫秒） |
| `LEASE_MIN_TTL_MS` | `1000` | 允许的最小 TTL（毫秒） |
| `LEASE_MAX_TTL_MS` | `600000` | 允许的最大 TTL（毫秒） |

在 `docker-compose.yml` 的 `api.environment` 中修改即可。

## API 一览（前缀 `/v1`）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/v1/leases/acquire` | 申请租约；被占用返回 `409` |
| `POST` | `/v1/leases/renew` | 续租；令牌/序号不匹配或已过期返回 `409` |
| `POST` | `/v1/leases/release` | 释放租约；不匹配返回 `409` |
| `POST` | `/v1/leases/validate` | 校验 (token, fencing) 是否仍为当前有效租约 |
| `GET` | `/v1/leases/{task_key}` | 查询租约状态（不暴露 token） |
| `GET` | `/healthz` | 健康检查（含 Redis 连通性） |

### 1. 申请租约

```bash
curl -s -X POST http://localhost:8000/v1/leases/acquire \
  -H 'Content-Type: application/json' \
  -d '{"task_key": "report-daily", "ttl_ms": 30000}'
```

成功（`200`）：

```json
{
  "acquired": true,
  "task_key": "report-daily",
  "token": "9f0b2c7a1e3d4c8f9a6b5d0e1f2a3b4c",
  "fencing_token": 7,
  "expire_at_ms": 1758556800123,
  "ttl_ms": 30000,
  "server_time_ms": 1758556770123
}
```

任务被他人持有（`409`），响应中包含当前租约的过期时间，可据此安排重试：

```json
{"detail": {"reason": "lease_held", "current_fencing_token": 7, "expire_at_ms": 1758556800123, "server_time_ms": 1758556770123}}
```

### 2. 续租

```bash
curl -s -X POST http://localhost:8000/v1/leases/renew \
  -H 'Content-Type: application/json' \
  -d '{"task_key": "report-daily", "token": "<token>", "fencing_token": 7, "ttl_ms": 30000}'
```

失败原因（`409` 的 `detail.reason`）：`lease_not_found` / `token_mismatch` /
`fencing_mismatch` / `lease_expired`。

### 3. 释放租约

```bash
curl -s -X POST http://localhost:8000/v1/leases/release \
  -H 'Content-Type: application/json' \
  -d '{"task_key": "report-daily", "token": "<token>", "fencing_token": 7}'
```

### 4. 校验（下游判定操作是否仍属当前租约）

```bash
curl -s -X POST http://localhost:8000/v1/leases/validate \
  -H 'Content-Type: application/json' \
  -d '{"task_key": "report-daily", "token": "<token>", "fencing_token": 7}'
```

```json
{"valid": true, "reason": "ok", "current_fencing_token": 7, "expire_at_ms": 1758556800123, "server_time_ms": 1758556771123}
```

下游在执行受保护的写操作前调用本接口；`valid=false` 时必须放弃操作。
`reason` 取值：`ok` / `lease_not_found` / `lease_expired` / `token_or_fencing_mismatch`。

## 典型工作流

```
worker A                      lease-api                     Redis
   |  acquire(task, ttl=30s)      |                            |
   |----------------------------->|---- Lua: TIME + 检查占用 -->|
   |<---- {token, fencing=8} -----|                            |
   |                                                             
   |  每 10s: renew(token, fencing=8)  —— 保活                  
   |                                                             
   |  下游写入前: validate(token, fencing=8) —— valid=true 才执行 
   |                                                             
   |  完成后: release(token, fencing=8)                          
```

若 worker A 假死超过 TTL：worker B 的 `acquire` 成功并获得 `fencing=9`；
此后 A 的 `renew`/`release`/`validate` 全部失效，无法干扰 B 的租约。

## 设计要点

- **原子性**：acquire / renew / release / validate 各为一段 Lua 脚本（`app/lua.py`），
  在 Redis 单线程内整体执行，不存在 check-then-act 竞态。
- **服务器时间**：脚本内调用 `TIME` 获取 Redis 服务器毫秒时间计算过期，
  客户端时钟快慢不影响正确性。
- **fencing 单调性**：每个任务键一个 `INCR` 计数器（`lease:fencing:{task_key}`），
  永不重用、只增不减。
- **Redis 数据结构**：租约为 HASH `lease:lease:{task_key}`（token / fencing / expire_at）。

## 本地开发与测试

```bash
pip install -r requirements.txt
pip install pytest httpx

# 启动本地 Redis 后运行集成测试（默认连 127.0.0.1:6379 的 15 号库）
LEASE_TEST_REDIS_URL=redis://127.0.0.1:6379/15 pytest tests/ -v

# 本地起服务
LEASE_REDIS_URL=redis://127.0.0.1:6379/0 uvicorn app.main:app --port 8000
```

## 项目结构

```
app/
  main.py     # FastAPI 路由与生命周期
  store.py    # Redis 访问层（注册并调用 Lua 脚本）
  lua.py      # 全部 Lua 脚本（状态转换的唯一入口）
  schemas.py  # 请求/响应模型
  config.py   # 环境变量配置
tests/
  test_leases.py  # 集成测试（需真实 Redis）
Dockerfile
docker-compose.yml
requirements.txt
```

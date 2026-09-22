"""租约状态机的全部状态转换均以 Lua 脚本在 Redis 内原子执行。

过期判断统一使用 Redis 服务器时间（``TIME`` 命令），避免各客户端
时钟偏移导致的脑裂。时间单位：毫秒。

每个任务键对应两个 Redis key：
  - ``{prefix}lease:{task_key}``   HASH: token / fencing / expire_at
  - ``{prefix}fencing:{task_key}`` STRING: 单调递增的防栅序号计数器
"""

# 申请租约：仅当租约不存在或已过期时成功。
# KEYS[1] = lease key, KEYS[2] = fencing counter key
# ARGV[1] = ttl_ms, ARGV[2] = token
# 返回 {acquired(0/1), fencing, expire_at_ms, now_ms}
ACQUIRE_SCRIPT = """
local t = redis.call('TIME')
local now = t[1] * 1000 + math.floor(t[2] / 1000)
local expire_at = tonumber(redis.call('HGET', KEYS[1], 'expire_at') or 0)
if expire_at > now then
  local fencing = tonumber(redis.call('HGET', KEYS[1], 'fencing') or 0)
  return {0, fencing, expire_at, now}
end
local fencing = redis.call('INCR', KEYS[2])
local new_expire = math.floor(now + tonumber(ARGV[1]))
redis.call('HSET', KEYS[1], 'token', ARGV[2], 'fencing', fencing, 'expire_at', new_expire)
return {1, fencing, new_expire, now}
"""

# 续租：必须同时匹配 token 与 fencing，且租约尚未过期。
# KEYS[1] = lease key
# ARGV[1] = token, ARGV[2] = fencing, ARGV[3] = ttl_ms
# 成功返回 {1, expire_at_ms, now_ms}
# 失败返回 {0, reason, now_ms}，reason: 1=无租约 2=token不匹配 3=fencing不匹配 4=已过期
RENEW_SCRIPT = """
local t = redis.call('TIME')
local now = t[1] * 1000 + math.floor(t[2] / 1000)
local token = redis.call('HGET', KEYS[1], 'token')
if not token then
  return {0, 1, now}
end
if token ~= ARGV[1] then
  return {0, 2, now}
end
local fencing = tonumber(redis.call('HGET', KEYS[1], 'fencing') or -1)
if fencing ~= tonumber(ARGV[2]) then
  return {0, 3, now}
end
local expire_at = tonumber(redis.call('HGET', KEYS[1], 'expire_at') or 0)
if expire_at <= now then
  return {0, 4, now}
end
local new_expire = math.floor(now + tonumber(ARGV[3]))
redis.call('HSET', KEYS[1], 'expire_at', new_expire)
return {1, new_expire, now}
"""

# 释放：必须同时匹配 token 与 fencing；旧持有者无法释放新租约。
# KEYS[1] = lease key
# ARGV[1] = token, ARGV[2] = fencing
# 返回 1=已释放, 0=未释放（不匹配或不存在）
RELEASE_SCRIPT = """
local token = redis.call('HGET', KEYS[1], 'token')
if not token or token ~= ARGV[1] then
  return 0
end
local fencing = tonumber(redis.call('HGET', KEYS[1], 'fencing') or -1)
if fencing ~= tonumber(ARGV[2]) then
  return 0
end
redis.call('DEL', KEYS[1])
return 1
"""

# 校验：判断 (token, fencing) 是否仍是该任务的当前有效租约。
# KEYS[1] = lease key
# ARGV[1] = token, ARGV[2] = fencing
# 返回 {valid(0/1), current_fencing, expire_at_ms, now_ms}
VALIDATE_SCRIPT = """
local t = redis.call('TIME')
local now = t[1] * 1000 + math.floor(t[2] / 1000)
local token = redis.call('HGET', KEYS[1], 'token')
if not token then
  return {0, 0, 0, now}
end
local fencing = tonumber(redis.call('HGET', KEYS[1], 'fencing') or 0)
local expire_at = tonumber(redis.call('HGET', KEYS[1], 'expire_at') or 0)
local valid = 0
if token == ARGV[1] and fencing == tonumber(ARGV[2]) and expire_at > now then
  valid = 1
end
return {valid, fencing, expire_at, now}
"""

# 状态查询（只读）：返回当前租约内容与服务器时间。
# KEYS[1] = lease key
# 返回 {HGETALL 结果, now_ms}
STATUS_SCRIPT = """
local t = redis.call('TIME')
local now = t[1] * 1000 + math.floor(t[2] / 1000)
local data = redis.call('HGETALL', KEYS[1])
return {data, now}
"""

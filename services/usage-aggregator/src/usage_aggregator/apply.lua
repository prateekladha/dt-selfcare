--[[
Atomically apply ONE stream message to subscriber state.

Exactly-once on an at-least-once broker:
  * stream offset is stored next to the state and checked first  -> replays after crash are no-ops
  * deterministic message id is remembered for a TTL             -> re-published chunks are no-ops
  * per-cycle, per-level alert flags (HSETNX)                    -> each alert is raised once
  * alerts are written to an outbox stream in the SAME script    -> no "state saved, alert lost"

All keys share the hash tag {pN} of the stream partition, so on a sharded Valkey cluster the
whole script still runs on one node atomically.

KEYS[1] offset key   KEYS[2] seen-message key   KEYS[3] alert outbox stream
KEYS[4+2i] usage hash (per subscriber+cycle)   KEYS[5+2i] profile hash (per subscriber)
ARGV: offset, msg_id, seen_ttl, usage_ttl, levels_csv, n_groups,
      then per group: subscriber, cycle, country, n_fields, (field, increment)*
--]]
local offset = tonumber(ARGV[1])
local current = tonumber(redis.call('GET', KEYS[1]) or '-1')
if offset <= current then
  return {0, 0, 'stale_offset'}
end
if redis.call('EXISTS', KEYS[2]) == 1 then
  redis.call('SET', KEYS[1], offset)
  return {0, 0, 'duplicate_message'}
end

local usage_ttl = tonumber(ARGV[4])
local levels = {}
for l in string.gmatch(ARGV[5], '([^,]+)') do table.insert(levels, tonumber(l)) end

local n_groups = tonumber(ARGV[6])
local a = 7
local alerts = 0
for g = 0, n_groups - 1 do
  local ukey, pkey = KEYS[4 + 2 * g], KEYS[5 + 2 * g]
  local sub, cycle, country, n_fields = ARGV[a], ARGV[a + 1], ARGV[a + 2], tonumber(ARGV[a + 3])
  a = a + 4
  local addon_changed = false
  for _ = 1, n_fields do
    local field, inc = ARGV[a], tonumber(ARGV[a + 1])
    a = a + 2
    redis.call('HINCRBY', ukey, field, inc)
    if field == 'addon_bytes' then addon_changed = true end
  end
  redis.call('EXPIRE', ukey, usage_ttl)

  local used = tonumber(redis.call('HGET', ukey, 'DATA') or '0')
             + tonumber(redis.call('HGET', ukey, 'ROAMING_DATA:EU') or '0')
  local allowance = tonumber(redis.call('HGET', pkey, 'allowance_bytes') or '0')
                  + tonumber(redis.call('HGET', ukey, 'addon_bytes') or '0')
  if allowance > 0 then
    local pct = used * 100 / allowance
    for _, level in ipairs(levels) do
      local flag = 'alert:' .. level
      if pct >= level then
        if redis.call('HSETNX', ukey, flag, offset) == 1 then
          redis.call('XADD', KEYS[3], 'MAXLEN', '~', 1000000, '*',
            'alert_id', sub .. ':' .. cycle .. ':' .. level, 'subscriber_id', sub, 'country', country,
            'cycle', cycle, 'level', level, 'used_bytes', string.format('%.0f', used),
            'allowance_bytes', string.format('%.0f', allowance), 'pct', string.format('%.1f', pct))
          alerts = alerts + 1
        end
      elseif addon_changed then
        -- add-on bought: allowance grew, so re-arm levels we are now below again
        redis.call('HDEL', ukey, flag)
      end
    end
  end
end

redis.call('SET', KEYS[2], 1, 'EX', tonumber(ARGV[3]))
redis.call('SET', KEYS[1], offset)
return {1, alerts, 'applied'}

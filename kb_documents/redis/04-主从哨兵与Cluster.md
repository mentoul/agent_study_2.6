---
title: Redis 主从、Sentinel 与 Cluster 高可用实践
domain: Redis
level: 高级
tags: [主从复制, Sentinel, Cluster, 高可用, 故障转移]
last_updated: 2026-11-11
---

# Redis 主从、Sentinel 与 Cluster 高可用实践

> 一句话结论：主从解决读扩展与数据冗余，Sentinel 解决“单主自动选主”，Cluster 解决“容量与写扩展”；三者都建立在**异步复制**之上，故障切换仍可能丢写。

## 1. 主从复制

### 1.1 建立复制

在从节点执行：

```redis
REPLICAOF 10.0.0.11 6379
```

取消复制并提升为主：

```redis
REPLICAOF NO ONE
```

配置文件中：

```ini
replicaof 10.0.0.11 6379
masterauth "replication-password"
masteruser "repl"          # Redis 6+ ACL 用户
replica-read-only yes
replica-serve-stale-data yes
repl-backlog-size 256mb
repl-timeout 60
```

从节点默认只读。`replica-serve-stale-data yes` 表示与主节点断开时仍可返回旧数据；如果业务不能接受陈旧读，应改为 `no` 并做好错误处理。

### 1.2 全量同步与增量同步

- 首次连接或 backlog 不足时，主节点生成 RDB，从节点加载后再接收增量命令；
- 短暂断线时，若偏移量仍在 `repl-backlog` 内，可做部分重同步（PSYNC）；
- backlog 太小会导致频繁全量同步，备份/恢复期间网络与磁盘压力很大。

```redis
INFO replication
ROLE
```

关注：

- `master_link_status:up`
- `master_last_io_seconds_ago`
- `slave_repl_offset` 与主节点 `master_repl_offset` 的差距
- `master_sync_in_progress`

### 1.3 复制的局限

Redis 复制是**异步**的：主节点执行完命令就返回客户端，不等待从节点确认。主节点宕机时，尚未发送的写会丢失；切换前从节点也可能没收到最新写。

`WAIT` 可以等待指定数量的副本确认：

```redis
SET order:1001 paid
WAIT 1 1000
```

但 `WAIT` 只表示“副本已收到”，不保证持久化，也不保证故障时该副本一定被选为主。它不能替代数据库事务或共识协议。

## 2. Sentinel：单主自动故障转移

### 2.1 架构

```text
          ┌────────────┐
          │ Sentinel 1 │
          └─────┬──────┘
                │ 监控/投票
┌─────────┐  ┌──▼──────┐  ┌─────────┐
│ Redis M │◄─┤ Sentinel│─►│ Redis R │
└─────────┘  └─────────┘  └─────────┘
```

Sentinel 负责：

- 监控主从健康；
- 主观下线（SDOWN）与客观下线（ODOWN）；
- 选举领头 Sentinel；
- 选择新主、切换、通知客户端；
- 配置提供者（客户端向 Sentinel 查询当前主地址）。

Sentinel 本身不是代理，不转发数据；至少部署 3 个（奇数），分布在不同故障域。

### 2.2 配置示例

```ini
# sentinel.conf
port 26379
daemonize yes
logfile /var/log/redis/sentinel.log

sentinel monitor mymaster 10.0.0.11 6379 2
sentinel down-after-milliseconds mymaster 5000
sentinel failover-timeout mymaster 60000
sentinel parallel-syncs mymaster 1
sentinel auth-pass mymaster "replication-password"
```

- `quorum=2`：至少 2 个 Sentinel 认为主不可达，才进入客观下线；
- 真正执行 failover 还需要**多数 Sentinel 同意**，因此 3 个 Sentinel 可容忍 1 个故障；
- `down-after-milliseconds` 太短会误判，太长会拉长 RTO；
- `parallel-syncs` 控制切换后同时向新主同步的从节点数量，过大可能造成新主压力。

### 2.3 客户端如何发现新主

```bash
redis-cli -p 26379 SENTINEL get-master-addr-by-name mymaster
redis-cli -p 26379 SENTINEL masters
```

客户端应通过 Sentinel 获取地址，并在失败时重新查询；连接池要处理拓扑变化。不要在主从切换后继续使用缓存的旧主地址。

### 2.4 Split-brain 与数据丢失

网络分区时可能出现：

- Sentinel 侧认为主库挂了，提升从库；
- 旧主仍在原网络内接受写入；
- 分区恢复后，旧主被降级为从库，其未同步的写被丢弃。

缓解：

```ini
# 主节点至少要有 N 个可用副本，且延迟不超过 M 秒，否则拒绝写入
min-replicas-to-write 1
min-replicas-max-lag 10
```

这是**可用性与一致性的权衡**：副本不足时主节点会拒绝写，业务可能不可用，但能降低脑裂丢数据风险。旧版本参数名为 `min-slaves-to-write` / `min-slaves-max-lag`。

还需要：

- 可靠的 fencing/隔离，确保旧主不能再被客户端写入；
- 应用侧只使用 Sentinel 返回的当前主；
- 监控切换事件与丢失窗口；
- 对账与补偿机制。

### 2.5 手工演练

```bash
# 查看状态
redis-cli -p 26379 SENTINEL masters

# 主动触发切换（演练/维护）
redis-cli -p 26379 SENTINEL failover mymaster
```

演练要验证：应用重连、连接池刷新、只读副本追平、告警、数据一致性。

## 3. Cluster：分片与写扩展

### 3.1 核心模型

Redis Cluster 将 key 空间分为 **16384 个槽（slot）**：

```text
slot = CRC16(key) mod 16384
```

每个 master 负责一部分槽；每个 master 可以有多个 replica。客户端收到 `MOVED` 后更新本地槽映射；迁移过程中可能收到 `ASK`，需要先发送 `ASKING` 再执行命令。

```bash
redis-cli -c -h 10.0.0.11 -p 6379
redis-cli -h 10.0.0.11 -p 6379 CLUSTER INFO
redis-cli -h 10.0.0.11 -p 6379 CLUSTER NODES
redis-cli -h 10.0.0.11 -p 6379 CLUSTER SLOTS
redis-cli -h 10.0.0.11 -p 6379 CLUSTER SHARDS
```

### 3.2 最小部署

生产至少 **3 主 3 从**，分布在不同宿主机/可用区：

```bash
redis-cli --cluster create \
  10.0.0.11:6379 10.0.0.12:6379 10.0.0.13:6379 \
  10.0.0.14:6379 10.0.0.15:6379 10.0.0.16:6379 \
  --cluster-replicas 1
```

配置：

```ini
cluster-enabled yes
cluster-config-file nodes.conf
cluster-node-timeout 15000
cluster-announce-ip 10.0.0.11
cluster-announce-port 6379
cluster-announce-bus-port 16379
appendonly yes
```

注意：

- 集群总线端口默认是数据端口 + 10000，防火墙必须放行；
- 容器/NAT 环境必须正确设置 `cluster-announce-*`，否则节点互相发现失败；
- `cluster-node-timeout` 影响故障检测与切换时间，过小易误判，过大 RTO 变长；
- 每个节点都要配置持久化与合理的 `maxmemory`。

### 3.3 多 key 限制

以下操作要求所有 key 在同一个槽：

- `MGET` / `MSET`；
- `SINTERSTORE` 等多 key 命令；
- `MULTI/EXEC` 事务；
- Lua 脚本中的多个 key；
- Stream 的跨 key 操作。

使用 **hash tag** 强制同槽：

```text
order:{1001}:detail
order:{1001}:items
```

`{1001}` 参与哈希计算，两个 key 会落在同一槽。代价是同一 tag 的数据集中在一个节点，可能形成热槽。

### 3.4 故障转移与可用性

- master 故障后，其 replica 通过集群总线选举提升；
- 需要多数 master 存活才能完成选举；
- `cluster-require-full-coverage yes`（默认）表示只要有槽不可用，整个集群拒绝服务；设为 `no` 可让可用槽继续服务，但访问故障槽仍会报错；
- 异步复制意味着切换可能丢写；
- 集群不提供跨槽强一致事务，不应作为核心账务的唯一存储。

```ini
cluster-require-full-coverage no
```

该配置是可用性偏好，不是数据安全措施。

### 3.5 扩缩容与迁移

```bash
# 检查集群
redis-cli --cluster check 10.0.0.11:6379

# 重新分片：把 1000 个槽从节点 A 迁到节点 B
redis-cli --cluster reshard 10.0.0.11:6379 \
  --cluster-from <source-node-id> \
  --cluster-to <target-node-id> \
  --cluster-slots 1000 \
  --cluster-yes

# 均衡槽位
redis-cli --cluster rebalance 10.0.0.11:6379

# 修复（谨慎，先备份）
redis-cli --cluster fix 10.0.0.11:6379
```

迁移期间：

- 客户端必须正确处理 `MOVED`/`ASK`；
- 大 key 迁移会造成明显阻塞与网络流量；
- 避免业务高峰扩容；
- 迁移后验证槽覆盖、副本关系、数据量与延迟。

### 3.6 手工故障转移

```bash
# 在希望提升的 replica 上执行
redis-cli -h 10.0.0.14 -p 6379 CLUSTER FAILOVER
```

`CLUSTER FAILOVER` 会与主节点协调，尽量不丢数据；`FORCE` 和 `TAKEOVER` 更激进，可能导致数据丢失，只应在灾难场景使用。

## 4. Sentinel、Cluster 与 Proxy 怎么选

| 方案 | 适用场景 | 优点 | 局限 |
| --- | --- | --- | --- |
| 主从 + Sentinel | 单主容量足够，需要自动切换 | 架构简单、客户端兼容好 | 写无法水平扩展；异步复制丢写窗口 |
| Redis Cluster | 数据量大、写 QPS 高 | 原生分片、自动故障转移 | 多 key 限制、客户端需支持集群协议、运维复杂 |
| Proxy 层 | 想屏蔽集群细节/统一路由 | 应用简单、可做读写分离 | 多一跳、代理成为瓶颈/单点，需高可用 |
| 云托管 | 希望减少运维 | 自动备份、监控、切换 | 受限于厂商能力与网络，仍需理解 SLA |

Twemproxy、Codis 等早期代理方案已逐步退出主流，新项目优先选择支持 Cluster 的客户端（如 Lettuce、Jedis、redis-py 的 cluster 模式）或云厂商代理。

## 5. 运维与监控

### 5.1 关键指标

```redis
INFO replication
INFO cluster
INFO memory
INFO persistence
```

- 主从偏移量差距、复制断开时长；
- Sentinel 的 `odown` 数量、最近 failover 时间；
- Cluster 的 `cluster_state`、槽覆盖、`known_nodes`、失败节点；
- 内存使用率、碎片率、淘汰数量；
- 命令延迟 P99、慢日志、连接数；
- AOF/RDB 状态与磁盘空间。

### 5.2 安全

```ini
bind 10.0.0.11
protected-mode yes
requirepass "strong-password"
masterauth "strong-password"
```

更推荐 ACL：

```redis
ACL SETUSER app on >password ~app:* +@read +@write -@dangerous
```

并启用 TLS（`tls-port`、`tls-cert-file`、`tls-key-file`、`tls-ca-cert-file`）。不要把 Redis 暴露在公网；`rename-command` 不能替代网络隔离与 ACL。

### 5.3 升级与变更

- 先升级 replica，再执行 `CLUSTER FAILOVER` 切换，最后升级旧 master；
- Sentinel 逐个升级，保持多数派在线；
- 升级前确认客户端兼容性、持久化格式、模块/命令变化；
- 变更窗口内加强延迟与错误率监控；
- 保留回滚版本与数据备份。

## 6. 故障演练清单

- [ ] 杀掉一个 replica：读是否正常，是否触发告警？
- [ ] 杀掉 master：Sentinel/Cluster 是否在目标 RTO 内完成切换？
- [ ] 网络分区：旧 master 是否被隔离，是否出现双写？
- [ ] 切换后客户端是否自动刷新拓扑？
- [ ] 新 master 的持久化与备份是否正常？
- [ ] 数据丢失窗口是否可量化、可接受？
- [ ] 恢复旧节点时是否避免脏数据回灌？

## 7. 小结

Redis 高可用的核心不是“有没有 Sentinel/Cluster”，而是**故障检测是否准确、选主是否安全、客户端是否能切换、丢写窗口是否可接受**。单主容量够就用主从 + Sentinel；需要分片就用 Cluster，并接受多 key 限制与运维复杂度。任何方案上线前，都必须做真实故障演练。

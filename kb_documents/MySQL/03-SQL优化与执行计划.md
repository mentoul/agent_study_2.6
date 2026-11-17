---
title: SQL 优化与执行计划：从慢日志到可验证的改写
domain: MySQL
level: 进阶
tags: [SQL优化, 执行计划, 慢查询, 索引]
last_updated: 2026-11-11
---

# SQL 优化与执行计划：从慢日志到可验证的改写

> 一句话结论：SQL 优化是一门**以测量为起点、以验证为终点**的工程活动；没有执行计划、真实数据量和前后对比的“优化”，大概率只是换了一种写法。

## 1. 建立优化闭环

推荐固定为六步：

1. **定义问题**：接口 P99、数据库 CPU、扫描行数还是锁等待？不同目标对应不同手段。
2. **采集证据**：慢日志、`performance_schema`、`EXPLAIN`、监控指标。
3. **定位瓶颈**：是扫描太多、回表太多、排序/临时表、锁冲突，还是网络/应用层问题。
4. **提出假设**：例如“缺少联合索引导致回表 20 万行”。
5. **小范围验证**：在接近生产的数据量和分布上对比。
6. **上线观察**：关注延迟、QPS、写入放大、复制延迟和缓存命中率，保留回滚方案。

不要一开始就改 SQL。先确认慢在数据库，还是慢在连接池、网络、序列化、下游依赖。

## 2. 打开并分析慢日志

```ini
[mysqld]
slow_query_log = ON
slow_query_log_file = /var/log/mysql/slow.log
long_query_time = 0.5
log_slow_admin_statements = ON
log_slow_replica_statements = ON
min_examined_row_limit = 100
```

`long_query_time` 可设为 0 临时采集全量语句，但不要长期开启。`log_queries_not_using_indexes` 在小表上会产生大量噪声，慎用。

```sql
SHOW VARIABLES LIKE 'slow_query_log%';
SHOW VARIABLES LIKE 'long_query_time';
```

使用 `pt-query-digest` 聚合：

```bash
pt-query-digest /var/log/mysql/slow.log \
  --since '2026-11-01 00:00:00' \
  --until '2026-11-11 00:00:00' \
  > slow-report.txt
```

报告重点看：总耗时占比、调用次数、平均/P95 延迟、扫描行数、返回行数、锁等待时间。**一条 SQL 慢但调用少，和一条 SQL 不算慢但每秒调用上万次，优先级完全不同。**

MySQL 8 也可直接查 `performance_schema`：

```sql
-- 按总延迟排序的语句摘要
SELECT DIGEST_TEXT, COUNT_STAR,
       ROUND(SUM_TIMER_WAIT/1e12, 3) AS total_sec,
       ROUND(AVG_TIMER_WAIT/1e9, 3) AS avg_ms,
       SUM_ROWS_EXAMINED, SUM_ROWS_SENT
FROM performance_schema.events_statements_summary_by_digest
ORDER BY SUM_TIMER_WAIT DESC
LIMIT 20;

-- sys 库的易读视图
SELECT * FROM sys.statement_analysis LIMIT 20;
SELECT * FROM sys.statements_with_full_table_scans LIMIT 20;
```

## 3. 读懂 EXPLAIN

```sql
EXPLAIN
SELECT o.id, o.amount, u.name
FROM orders o
JOIN users u ON u.id = o.user_id
WHERE o.status = 1
  AND o.created_at >= '2026-11-01'
ORDER BY o.created_at DESC
LIMIT 50;
```

### 3.1 访问类型（`type`）

从好到差的常见顺序：

```text
system > const > eq_ref > ref > range > index > ALL
```

- `const`：主键/唯一索引等值命中至多一行；
- `eq_ref`：连接中每次只匹配一行，通常是主键/唯一索引；
- `ref`：非唯一索引等值匹配；
- `range`：索引范围扫描；
- `index`：扫描整棵索引树（比全表轻，但仍是全量）；
- `ALL`：全表扫描。

`type` 不是唯一标准。小表全扫可能比走索引更快；返回大量行的查询，优化器选择全扫是合理的。

### 3.2 `rows`、`filtered` 与 `key_len`

- `rows` 是估算的扫描行数，不是返回行数；
- `filtered` 是扫描后剩余的估算比例；
- `key_len` 可判断联合索引使用了几列，但受字符集、是否可空、变长类型影响，不能简单按“列数”读。

### 3.3 `Extra` 关键信号

| 信息 | 含义 | 处理方向 |
| --- | --- | --- |
| `Using index` | 覆盖索引 | 通常较好 |
| `Using index condition` | 索引条件下推 | 减少回表，但仍可能回表 |
| `Using where` | 存储引擎返回后再过滤 | 检查是否可把条件下推到索引 |
| `Using temporary` | 使用临时表 | 常见于 `GROUP BY`、`DISTINCT`、复杂排序 |
| `Using filesort` | 需要额外排序 | 考虑让索引顺序满足 `ORDER BY` |
| `Using join buffer` | 连接无可用索引 | 检查连接列索引与连接顺序 |

`Using filesort` 不一定是磁盘排序，也可能在内存中完成；数据量大时才真正昂贵。

### 3.4 真实执行：`EXPLAIN ANALYZE`

```sql
EXPLAIN ANALYZE
SELECT ...
```

它会实际执行语句并给出各算子耗时、实际行数与循环次数。对比 `EXPLAIN` 的估算行数和 `EXPLAIN ANALYZE` 的实际行数，可以快速发现统计信息失真或数据倾斜。**对写语句使用前务必在测试库确认。**

### 3.5 优化器为什么选错索引

可能原因：

- 统计信息过期或采样偏差：`ANALYZE TABLE`；
- 数据分布倾斜，估算值与实际值差距大；
- 索引列参与函数、隐式转换；
- 回表成本估算与实际 Buffer Pool 命中率不符；
- 索引过多，优化器评估成本出现偏差。

可以临时用 `FORCE INDEX` 验证假设，但**不要把强制索引当作长期修复**；优先修正统计信息、SQL 写法或索引设计。

```sql
-- 查看优化器候选计划与成本（会话级）
SET optimizer_trace = 'enabled=on';
SELECT ...;
SELECT * FROM information_schema.OPTIMIZER_TRACE\G
SET optimizer_trace = 'enabled=off';
```

## 4. 连接与排序的常见问题

### 4.1 连接列类型/字符集不一致

`utf8mb4` 与 `utf8mb3`、字符串与数字、不同排序规则之间的连接，可能导致索引失效或隐式转换。建表时统一字符集和列类型，连接列必须建索引。

### 4.2 小表驱动大表，但别教条

MySQL 优化器会基于成本选择连接顺序。一般让小结果集驱动大表，但如果连接列无索引，可能退化为块嵌套循环（BNL）或哈希连接。MySQL 8.0.18+ 在合适场景支持哈希连接，能改善无索引等值连接，但**不能替代正确的索引设计**。

### 4.3 排序与临时表

```sql
-- 联合索引 (user_id, created_at) 可同时支持过滤与排序
SELECT id, amount
FROM orders
WHERE user_id = 42
ORDER BY created_at DESC
LIMIT 20;
```

如果 `WHERE` 用 `user_id`、`ORDER BY` 用 `created_at`，但索引是 `(created_at, user_id)`，则无法直接利用索引顺序，可能产生 filesort。联合索引列顺序要匹配查询模式。

## 5. 十类高频改写

### 5.1 避免 `SELECT *`

只取需要的列，降低网络传输、内存和临时表开销，也提高覆盖索引概率。

### 5.2 深分页

```sql
-- 慢：扫描并丢弃前 100 万行
SELECT * FROM orders ORDER BY id LIMIT 1000000, 20;

-- 快：基于游标
SELECT * FROM orders WHERE id > 1000000 ORDER BY id LIMIT 20;

-- 需要按非唯一列排序时，用“列 + 唯一列”做游标
SELECT id, created_at
FROM orders
WHERE (created_at, id) < ('2026-11-01 12:00:00', 900000)
ORDER BY created_at DESC, id DESC
LIMIT 20;
```

### 5.3 用 `EXISTS` 替代低效的 `IN` 子查询

```sql
-- 根据执行计划选择，不要盲目替换
SELECT u.id, u.name
FROM users u
WHERE EXISTS (
  SELECT 1 FROM orders o WHERE o.user_id = u.id AND o.status = 1
);
```

MySQL 8 会对部分子查询做半连接优化。最终以 `EXPLAIN ANALYZE` 为准。

### 5.4 大 `IN` 列表

上千个值的 `IN` 会带来解析、优化和网络开销。可改为临时表 + `JOIN`，或分批查询。注意 `IN` 中的值类型要与列一致，避免隐式转换。

### 5.5 函数与表达式

```sql
-- 无法使用 created_at 索引
WHERE DATE(created_at) = '2026-11-11';

-- 可使用范围索引
WHERE created_at >= '2026-11-11 00:00:00'
  AND created_at <  '2026-11-12 00:00:00';
```

### 5.6 前导通配符

`LIKE '%foo%'` 无法做 B+ 树范围定位。可评估：

- 全文索引（InnoDB FULLTEXT）；
- 搜索引擎/倒排索引；
- 维护反转字段、分词字段或前缀字段；
- 业务上限制为后缀匹配 `LIKE 'foo%'`。

### 5.7 `ORDER BY RAND()`

会为全表生成随机数并排序，大表上代价极高。常见替代：按 ID 范围随机取、预生成随机键、应用层抽样。

### 5.8 大批量更新/删除

一次性更新百万行会造成长事务、大量 undo、复制延迟和锁竞争。改为按主键分批：

```sql
DELETE FROM logs
WHERE created_at < '2026-01-01'
ORDER BY id
LIMIT 1000;
-- 循环执行，每批之间短暂休眠，并监控复制延迟
```

### 5.9 统计类查询

`COUNT(*)` 在 InnoDB 中需要扫描索引，大表上不可能 O(1)。若业务允许近似值，可使用缓存、计数表或预聚合；需要精确值时，尽量缩小范围并利用覆盖索引。

### 5.10 不必要的 `DISTINCT` / `GROUP BY`

先确认数据是否真的重复。为了“保险”加 `DISTINCT` 可能引入临时表和排序。用 `EXPLAIN` 验证去重是否必要。

## 6. 索引与 SQL 的联合优化

一个实用流程：

```sql
-- 1. 看计划
EXPLAIN ANALYZE SELECT ...;

-- 2. 看索引
SHOW INDEX FROM orders;

-- 3. 看数据分布
SELECT status, COUNT(*) FROM orders GROUP BY status;

-- 4. 建候选索引（测试环境）
ALTER TABLE orders ADD INDEX idx_status_created (status, created_at);

-- 5. 再对比
EXPLAIN ANALYZE SELECT ...;
```

注意写入放大：每个二级索引都会增加 INSERT/UPDATE/DELETE 的成本。读优化不能以牺牲整体写入吞吐为代价。

## 7. 上线检查清单

- [ ] 是否用生产近似数据量和分布验证？
- [ ] 是否对比了 P50/P95/P99，而不只是单次耗时？
- [ ] 是否检查了 Buffer Pool 命中率、临时表落盘、复制延迟？
- [ ] 新索引是否导致写入性能明显下降？
- [ ] 是否有回滚方案（删索引、回退 SQL）？
- [ ] 是否更新了慢查询基线和告警阈值？

## 8. 小结

SQL 优化的核心不是“记住多少种改写技巧”，而是建立证据链：**慢在哪里 → 计划如何 → 实际执行多少行 → 改写后是否真的更快 → 对写入和复制有什么副作用**。只要坚持这个闭环，绝大多数性能问题都能被拆解成可验证的小问题。

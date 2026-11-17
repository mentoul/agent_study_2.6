---
title: MySQL 事务、MVCC 与锁：并发问题定位手册
domain: MySQL
level: 进阶
tags: [事务, MVCC, 锁, 死锁, InnoDB]
last_updated: 2026-11-11
---

# MySQL 事务、MVCC 与锁：并发问题定位手册

> 一句话结论：InnoDB 的并发控制由**多版本读（MVCC）**和**两阶段锁（2PL）**共同完成；先判断是“快照读还是当前读”，再判断“锁住了哪条索引记录”，大多数困惑会迎刃而解。

## 1. 事务的四个特性与实现手段

| 特性 | 含义 | InnoDB 的主要实现 |
| --- | --- | --- |
| 原子性 A | 要么全部成功，要么全部回滚 | undo log |
| 一致性 C | 约束、业务规则不被破坏 | 应用 + 数据库约束 + 事务 |
| 隔离性 I | 并发事务互不干扰 | MVCC + 锁 |
| 持久性 D | 提交后不丢失 | redo log + 刷盘 + 双写缓冲 |

需要强调：数据库保证的是事务语义，不是业务语义。“先查库存再扣减”如果写成两条独立 SQL，即使各自在事务中，也可能发生并发覆盖，必须使用条件更新、行锁或乐观锁版本号。

## 2. 四种隔离级别

| 隔离级别 | 脏读 | 不可重复读 | 幻读（快照读） | 备注 |
| --- | --- | --- | --- | --- |
| READ UNCOMMITTED | 可能 | 可能 | 可能 | 很少使用 |
| READ COMMITTED | 否 | 可能 | 可能 | 每条语句生成新 ReadView |
| REPEATABLE READ | 否 | 否 | InnoDB 下通常避免 | 默认级别，首次快照读时生成 ReadView |
| SERIALIZABLE | 否 | 否 | 否 | 读也会加锁，并发度低 |

InnoDB 的 RR 并不能在所有场景下完全消除“幻读”的直觉现象：快照读看到的是事务开始时的版本；当前读（`FOR UPDATE`、`FOR SHARE`、`UPDATE`、`DELETE`）读取最新版本并加锁。如果同一事务先快照读、再当前读，结果可能不同。

```sql
-- 查看当前隔离级别
SELECT @@transaction_isolation;

-- 会话级切换为 RC（务必确认业务与复制兼容性）
SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED;
```

RC 能减少间隙锁和锁冲突，适合高并发写入；RR 提供更强的语句间一致性。选择应基于业务语义和压测结果，而不是“RC 一定更快”。

## 3. MVCC 如何判断版本可见性

InnoDB 聚簇索引的隐藏列包括：

- `DB_TRX_ID`：最后修改该行的事务 ID；
- `DB_ROLL_PTR`：指向 undo log 的版本链指针；
- `DB_ROW_ID`：无主键时生成的隐藏行 ID。

一次快照读会创建 ReadView，其中包含：

- `m_ids`：创建时仍活跃的读写事务 ID 集合；
- `min_trx_id`：活跃事务中的最小 ID；
- `max_trx_id`：下一个将分配的事务 ID；
- `creator_trx_id`：当前事务 ID。

简化后的可见性判断：

1. 版本由当前事务修改：可见；
2. `trx_id < min_trx_id`：事务已提交，可见；
3. `trx_id >= max_trx_id`：事务在 ReadView 之后才开始，不可见；
4. `trx_id` 在 `m_ids` 中：仍活跃，不可见；
5. 其余情况：已提交，可见。

不可见时沿着 `DB_ROLL_PTR` 找更早的 undo 版本，直到找到可见版本。这就是“读不加锁也能读到一致快照”的原因。

**长事务的危害**：旧 ReadView 会阻止 purge 清理旧版本，undo 表空间持续膨胀，历史版本链变长，查询变慢，甚至拖垮磁盘。监控长事务应是日常运维项。

```sql
SELECT trx_id, trx_state, trx_started,
       TIMESTAMPDIFF(SECOND, trx_started, NOW()) AS age_sec,
       trx_mysql_thread_id, trx_query
FROM information_schema.INNODB_TRX
ORDER BY trx_started;
```

## 4. 快照读与当前读

| 类型 | 示例 | 读取版本 | 是否加锁 |
| --- | --- | --- | --- |
| 快照读 | 普通 `SELECT` | ReadView 对应版本 | 不加行锁 |
| 当前读 | `SELECT ... FOR UPDATE` | 最新已提交版本 | 加排他锁 |
| 当前读 | `SELECT ... FOR SHARE` | 最新已提交版本 | 加共享锁 |
| 当前读 | `UPDATE` / `DELETE` | 最新版本 | 加排他锁 |

RC 下每条语句创建新 ReadView；RR 下同一事务的第一次快照读创建 ReadView，后续快照读复用。注意：事务启动（`START TRANSACTION`）本身不等于创建 ReadView，RR 的 ReadView 通常在第一次一致性读时才建立。

## 5. InnoDB 锁的类型

### 5.1 按模式

- **共享锁 S**：`SELECT ... FOR SHARE`，多个事务可同时持有；
- **排他锁 X**：`SELECT ... FOR UPDATE`、`UPDATE`、`DELETE`，互斥。

### 5.2 按粒度

- **记录锁（Record Lock）**：锁住索引记录本身；
- **间隙锁（Gap Lock）**：锁住两条索引记录之间的“空隙”，阻止插入；
- **临键锁（Next-Key Lock）**：记录锁 + 前面的间隙，RR 下范围扫描常见；
- **插入意向锁（Insert Intention Lock）**：插入前设置，和间隙锁冲突；
- **意向锁（IS/IX）**：表级标志，表示事务准备在行级加 S/X 锁，用于快速判断表锁冲突。

关键点：**InnoDB 锁的是索引记录，不是“行”这个概念**。如果查询没有走索引，可能扫描并锁住大量记录甚至全表，这是很多“只更新一行却锁全表”问题的根源。

### 5.3 唯一索引等值查询的特殊情况

在 RR 下，唯一索引等值命中已存在记录时，通常只加记录锁，不加间隙锁；若记录不存在，则会加间隙锁以防止其他事务插入。非唯一索引等值查询通常会继续向右扫描到不满足条件的记录，因此可能产生 next-key lock。

具体加锁范围受版本、索引、数据分布和执行计划影响，**不要只背结论，要用 `performance_schema.data_locks` 验证**。

## 6. 死锁：从复现到处理

### 6.1 一个典型死锁

```sql
-- 会话 A
BEGIN;
UPDATE accounts SET balance = balance - 100 WHERE id = 1;
-- 此处不提交

-- 会话 B
BEGIN;
UPDATE accounts SET balance = balance - 100 WHERE id = 2;
-- 此处不提交

-- 会话 A
UPDATE accounts SET balance = balance + 100 WHERE id = 2; -- 等待 B

-- 会话 B
UPDATE accounts SET balance = balance + 100 WHERE id = 1; -- 死锁，InnoDB 回滚其中一个事务
```

两个事务以相反顺序持有并申请锁，形成环。InnoDB 默认开启死锁检测，会快速回滚代价较小的事务，并返回：

```text
ERROR 1213 (40001): Deadlock found when trying to get lock;
try restarting transaction
```

### 6.2 诊断

```sql
-- 当前事务
SELECT * FROM information_schema.INNODB_TRX\G

-- 当前锁与等待关系（MySQL 8.0）
SELECT * FROM performance_schema.data_locks\G
SELECT * FROM performance_schema.data_lock_waits\G

-- 更易读的等待视图
SELECT * FROM sys.innodb_lock_waits\G

-- 最近一次死锁详情
SHOW ENGINE INNODB STATUS\G
```

建议开启全量死锁日志，便于事后分析：

```ini
[mysqld]
innodb_print_all_deadlocks = ON
```

日志中重点关注 `LATEST DETECTED DEADLOCK` 段的两条事务 SQL、持有锁、等待锁和索引信息。

### 6.3 应用层处理原则

1. **捕获 1213 并重试**：只重试整个事务，不要重试单条语句；设置最大次数和指数退避。
2. **捕获 1205 锁等待超时**：`innodb_lock_wait_timeout` 默认 50 秒，OLTP 场景通常应调小（如 3～10 秒），避免请求长期挂起。
3. **统一加锁顺序**：例如总是按主键升序更新账户，能显著降低环路概率。
4. **缩短事务**：把 RPC、文件上传、消息发送等慢操作移出事务。
5. **合理使用乐观锁**：版本号或条件更新，避免长时间持锁。
6. **避免无索引更新**：确认执行计划使用索引，减少锁范围。

```sql
-- 乐观锁扣减库存：一条语句完成判断与更新
UPDATE inventory
SET stock = stock - 1, version = version + 1
WHERE sku_id = 1001 AND stock >= 1;

-- affected_rows = 0 表示库存不足或并发冲突，由业务决定重试/失败
```

## 7. 元数据锁与 DDL 阻塞

除了行锁，DDL 还会涉及元数据锁（MDL）。一个长事务持有表的 MDL，后续 DDL 可能等待；DDL 等待期间，新来的查询又可能排在 DDL 后面，形成“连接堆积”。

```sql
SELECT * FROM performance_schema.metadata_locks;
SELECT * FROM sys.schema_table_lock_waits\G
```

生产变更建议：

- 使用 `ALGORITHM=INPLACE, LOCK=NONE` 并确认版本支持；
- 设置 `lock_wait_timeout`，避免无限等待；
- 在低峰期执行，先检查长事务；
- 对大表使用 gh-ost、pt-online-schema-change 等工具，并充分演练。

## 8. 事务设计检查表

- [ ] 事务边界是否清晰？是否包含网络调用、消息发送等不可控操作？
- [ ] 是否只访问必要的行？更新条件是否命中索引？
- [ ] 是否存在“先查后改”的竞态？能否改成条件更新或 `SELECT ... FOR UPDATE`？
- [ ] 多表/多行更新是否遵循统一顺序？
- [ ] 是否设置了合理的锁等待超时与重试策略？
- [ ] 是否有长事务监控与告警？
- [ ] 隔离级别是否符合业务预期？是否验证过 RC/RR 差异？

## 9. 小结

MVCC 解决“读多写少”的一致性读问题，锁解决“写写冲突”和当前读的互斥问题。定位并发问题时应按顺序确认：隔离级别 → 快照读/当前读 → 执行计划使用的索引 → 实际锁范围 → 事务持锁时长。把这几层拆开，死锁和锁等待就不再是黑盒。

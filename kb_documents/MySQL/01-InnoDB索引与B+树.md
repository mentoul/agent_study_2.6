---
title: InnoDB 索引与 B+ 树：从存储结构到索引设计
domain: MySQL
level: 进阶
tags: [InnoDB, B+树, 索引, 执行计划]
last_updated: 2026-11-11
---

# InnoDB 索引与 B+ 树：从存储结构到索引设计

> 一句话结论：索引设计不是“给 WHERE 列加索引”，而是围绕**访问路径、选择性、回表成本和写入成本**做整体权衡。

## 1. 为什么 InnoDB 选择 B+ 树

InnoDB 以页（默认 16 KB）为磁盘 I/O 的基本单位。B+ 树的特点是：

- 非叶子节点只存键和子页指针，单页可容纳更多键，树高通常只有 3～4 层；
- 所有数据都在叶子节点，叶子节点通过双向链表相连，适合范围扫描；
- 每次查询的 I/O 次数相对稳定，便于把热点页放在 Buffer Pool 中。

与 B 树相比，B+ 树把数据全部下沉到叶子层，非叶子层更“瘦”，同样页大小能覆盖更多行；与哈希索引相比，B+ 树支持范围、排序和前缀匹配。代价是写入时可能触发页分裂，索引越多，DML 维护成本越高。

## 2. 聚簇索引与二级索引

### 2.1 聚簇索引

如果表有主键，InnoDB 使用主键构造聚簇索引，叶子节点存放整行数据。没有主键时，会选择第一个 `NOT NULL` 的唯一索引；再没有就生成隐藏的 `DB_ROW_ID`。

由此得到两个实践原则：

1. **主键要短**。二级索引的叶子节点存的是主键值，主键越长，所有二级索引越大。
2. **主键尽量单调递增**。随机主键会造成插入点分散、页分裂和缓存命中率下降；UUID 可以考虑 `UUID_TO_BIN(uuid, 1)` 或雪花类有序 ID。

### 2.2 二级索引与回表

二级索引叶子节点存放“索引列 + 主键”。查询列不在索引中时，需要拿主键回到聚簇索引再查一次，这就是回表。

```sql
CREATE TABLE orders (
  id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  user_id     BIGINT UNSIGNED NOT NULL,
  status      TINYINT NOT NULL,
  amount      DECIMAL(12,2) NOT NULL,
  created_at  DATETIME(3) NOT NULL,
  PRIMARY KEY (id),
  KEY idx_user_status_created (user_id, status, created_at),
  KEY idx_created (created_at)
) ENGINE=InnoDB;
```

```sql
-- 覆盖索引：只访问 idx_user_status_created
EXPLAIN
SELECT user_id, status, created_at
FROM orders
WHERE user_id = 42 AND status = 1
ORDER BY created_at DESC
LIMIT 20;
```

如果 `Extra` 中出现 `Using index`，说明使用了覆盖索引；如果出现 `Using index condition`，说明使用了索引条件下推（ICP），但仍有回表可能。

## 3. 联合索引与最左前缀

联合索引 `(a, b, c)` 可以支持：

- `a = ?`
- `a = ? AND b = ?`
- `a = ? AND b = ? AND c = ?`
- `a = ? AND b > ?`（`c` 通常无法继续用于精确定位，但可用于 ICP 过滤）
- `a = ? ORDER BY b, c`

不能高效支持：

- 缺少 `a` 的查询；
- `a = ? AND c = ?` 中跳过 `b` 的精确匹配；
- `a = ? AND b LIKE '%x'` 之后继续用 `c` 定位。

设计联合索引时，通常把**等值条件列**放前面，把**范围/排序列**放后面；但不要机械套公式，要结合实际 SQL 与数据分布。

## 4. 选择性、基数与索引收益

选择性常用 `COUNT(DISTINCT col) / COUNT(*)` 衡量。性别、状态这类低基数列单独建索引往往收益很低，因为优化器可能判断全表扫描更便宜。

```sql
SELECT
  COUNT(*) AS total,
  COUNT(DISTINCT user_id) / COUNT(*) AS user_selectivity,
  COUNT(DISTINCT status) / COUNT(*) AS status_selectivity
FROM orders;
```

注意：

- `SHOW INDEX` 中的 `Cardinality` 是采样估算值，不一定精确；必要时执行 `ANALYZE TABLE`。
- “区分度高”不等于“这个查询值得建索引”。如果查询返回 40% 的行，顺序扫描可能更优。
- 联合索引的选择性要看组合，不要只看单列。

## 5. 读懂执行计划

```sql
EXPLAIN FORMAT=TREE
SELECT id, amount
FROM orders
WHERE user_id = 42 AND status = 1
ORDER BY created_at DESC
LIMIT 20;
```

MySQL 8.0.18 起可使用 `EXPLAIN ANALYZE` 获取真实执行耗时（会实际执行 SQL，写操作需谨慎）：

```sql
EXPLAIN ANALYZE
SELECT id, amount
FROM orders
WHERE user_id = 42 AND status = 1
ORDER BY created_at DESC
LIMIT 20;
```

重点关注：

| 字段 | 关注点 |
| --- | --- |
| `type` | `const`/`eq_ref`/`ref`/`range` 通常较好；`index` 是全索引扫描；`ALL` 是全表扫描 |
| `key` / `key_len` | 实际使用的索引及使用到的字节数，可判断联合索引用到了几列 |
| `rows` / `filtered` | 估算扫描行数与过滤比例，二者相乘可粗略估计参与后续操作的行数 |
| `Extra` | `Using index` 覆盖索引；`Using temporary`/`Using filesort` 需结合数据量判断 |

执行计划是估算，不等于事实。统计信息过期、数据倾斜、参数不同都会让计划变化。优化前后要用真实数据量对比 `EXPLAIN ANALYZE` 和慢日志。

## 6. 常见“索引失效”场景

1. **列上做函数或运算**：`WHERE DATE(created_at) = '2026-11-11'`。改为范围条件：
   ```sql
   WHERE created_at >= '2026-11-11 00:00:00'
     AND created_at <  '2026-11-12 00:00:00'
   ```
2. **隐式类型转换**：字符串列与数字比较，可能触发全表转换。确保参数类型与列类型一致。
3. **前导通配符**：`LIKE '%keyword'` 无法做索引范围定位。可考虑全文索引、倒排检索或额外的前缀/分词字段。
4. **`OR` 混用**：部分分支无索引时，整体可能退化为全表扫描。可拆成 `UNION ALL` 分别优化。
5. **深分页**：`LIMIT 1000000, 20` 需要扫描并丢弃大量行。使用延迟关联或基于游标的分页：
   ```sql
   -- 延迟关联
   SELECT o.*
   FROM orders o
   JOIN (
     SELECT id FROM orders
     WHERE status = 1
     ORDER BY created_at DESC
     LIMIT 1000000, 20
   ) t ON t.id = o.id;

   -- 游标分页（更推荐）
   SELECT id, amount, created_at
   FROM orders
   WHERE status = 1
     AND (created_at, id) < ('2026-11-01 12:00:00', 900000)
   ORDER BY created_at DESC, id DESC
   LIMIT 20;
   ```
6. **对索引列使用 `!=`、`NOT IN`、`IS NOT NULL`**：不是绝对不能用，而是优化器可能判断扫描比例过高而放弃索引。

## 7. 索引设计清单

- [ ] 先收集真实 SQL，而不是凭表结构猜索引。
- [ ] 单表索引数量通常控制在 5～7 个以内，写多读少的表更要克制。
- [ ] 联合索引优先覆盖高频、稳定的查询模式。
- [ ] 尽量用覆盖索引减少回表，但不要把大字段塞进索引。
- [ ] 长字符串使用前缀索引，并评估前缀的选择性。
- [ ] 主键短、有序；避免频繁更新主键。
- [ ] 上线前用生产近似数据验证；大表变更使用 Online DDL 并评估锁与复制延迟。
- [ ] 定期清理长期未使用的索引（可用 `sys.schema_unused_indexes` 辅助判断，但需覆盖完整业务周期）。

## 8. 排障命令速查

```sql
-- 查看索引
SHOW INDEX FROM orders;

-- 查看表状态与统计信息
SHOW TABLE STATUS LIKE 'orders';
ANALYZE TABLE orders;

-- 查看索引使用情况（需开启 performance_schema）
SELECT * FROM sys.schema_unused_indexes;

-- 查看冗余索引
SELECT * FROM sys.schema_redundant_indexes;
```

## 9. 小结

B+ 树决定了 InnoDB 的访问方式，聚簇索引决定了回表成本，统计信息决定了优化器的选择。好的索引设计应当从**真实查询、真实数据分布、真实写入比例**出发，最终以延迟、吞吐和稳定性指标验收，而不是以“有没有走索引”作为唯一标准。

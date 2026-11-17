"""检索评估脚本：Recall@K + MRR@K。

用法（在项目根目录）：
    python eval/eval_retrieval.py --project . --recall-k 20 --final-k 5

默认读取 eval/testset.csv：
    query,expected_file,domain,note

评估两套结果：
1. Bi 召回：store.query(query, top_k=recall_k)
2. 两阶段：two_stage_retrieve(query, store, recall_k, final_k)

判定标准：结果中只要有一条 chunk 的 metadata["source_file"]
等于 expected_file，就算命中；取第一次命中的排名计算 MRR。
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="RAG 检索评估")
    parser.add_argument(
        "--project",
        default=str(Path(__file__).resolve().parents[1]),
        help="项目根目录（包含 kb_engine.py 和 kb_documents/）",
    )
    parser.add_argument(
        "--testset",
        default=str(Path(__file__).resolve().parent / "testset.csv"),
        help="测试集 CSV 路径",
    )
    parser.add_argument("--recall-k", type=int, default=20, help="Bi 召回数量")
    parser.add_argument("--final-k", type=int, default=5, help="最终返回数量")
    parser.add_argument(
        "--out",
        default=str(Path(__file__).resolve().parent / "eval_results.csv"),
        help="逐条结果输出路径",
    )
    return parser.parse_args()


def first_rank(results: list[dict], expected_file: str) -> int | None:
    """返回第一次命中 expected_file 的排名（从 1 开始），没命中返回 None。"""
    for rank, item in enumerate(results, start=1):
        if item["metadata"].get("source_file") == expected_file:
            return rank
    return None


def main() -> None:
    args = parse_args()

    project = Path(args.project).resolve()
    if not (project / "kb_engine.py").is_file():
        raise SystemExit(f"找不到 {project / 'kb_engine.py'}，请用 --project 指定项目根目录")

    # 评估不需要 Langfuse，关掉可避免产生大量测试 trace
    os.environ["LANGFUSE_TRACING_ENABLED"] = "false"

    sys.path.insert(0, str(project))
    os.chdir(project)

    from kb_engine import SimpleVectorStore, load_kb, two_stage_retrieve  # noqa: E402

    with open(args.testset, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))

    print(f"测试集：{len(rows)} 条 | Bi Recall@{args.recall_k} | Final@{args.final_k}")

    print("加载知识库并编码向量（首次约 20–60 秒）...")
    t0 = time.time()
    chunks, stats = load_kb(
        root_dir=str(project / "kb_documents"),
        chunk_size=256,
        overlap=20,
        split_strategy="sentence",
    )
    store = SimpleVectorStore(dim=512)
    store.add(
        texts=[c["text"] for c in chunks],
        metadatas=[c["meta"] for c in chunks],
    )
    print(
        f"知识库：{stats['doc_count']} 篇 / {stats['chunk_count']} chunk | "
        f"编码耗时 {time.time() - t0:.1f}s"
    )

    out_rows: list[dict] = []
    bi_hits = 0
    final_hits = 0
    bi_rr_sum = 0.0
    final_rr_sum = 0.0
    bi_lat_sum = 0.0
    final_lat_sum = 0.0

    print("\n逐条结果")
    print("-" * 96)
    for i, row in enumerate(rows, start=1):
        query = row["query"].strip()
        expected = row["expected_file"].strip()

        t_bi = time.perf_counter()
        bi_results = store.query(query, top_k=args.recall_k)
        bi_latency = time.perf_counter() - t_bi
        bi_rank = first_rank(bi_results, expected)

        t_final = time.perf_counter()
        final_results = two_stage_retrieve(
            query, store, recall_k=args.recall_k, final_k=args.final_k
        )
        final_latency = time.perf_counter() - t_final
        final_rank = first_rank(final_results, expected)

        bi_lat_sum += bi_latency
        final_lat_sum += final_latency

        if bi_rank is not None:
            bi_hits += 1
            bi_rr_sum += 1.0 / bi_rank
        if final_rank is not None:
            final_hits += 1
            final_rr_sum += 1.0 / final_rank

        top1 = final_results[0]["metadata"] if final_results else {}
        top1_file = top1.get("source_file", "-")
        top1_domain = top1.get("domain", "-")

        bi_mark = "✓" if bi_rank else "✗"
        final_mark = "✓" if final_rank else "✗"
        print(
            f"[{i:02d}/{len(rows):02d}] Bi {bi_mark} #{bi_rank or '-'} | "
            f"Final {final_mark} #{final_rank or '-'} | "
            f"top1={top1_domain}/{top1_file} | {query[:42]}"
        )

        out_rows.append(
            {
                "query": query,
                "expected_file": expected,
                "bi_rank": bi_rank or "",
                "bi_hit": int(bi_rank is not None),
                "final_rank": final_rank or "",
                "final_hit": int(final_rank is not None),
                "final_top1_file": top1_file,
                "final_top1_domain": top1_domain,
                "bi_latency_s": round(bi_latency, 4),
                "final_latency_s": round(final_latency, 4),
            }
        )

    n = len(rows)
    print("-" * 96)
    print(f"Bi  Recall@{args.recall_k}: {bi_hits}/{n} = {bi_hits / n:.1%}")
    print(f"Bi  MRR@{args.recall_k}  : {bi_rr_sum / n:.4f}")
    print(f"Final Recall@{args.final_k}: {final_hits}/{n} = {final_hits / n:.1%}")
    print(f"Final MRR@{args.final_k}  : {final_rr_sum / n:.4f}")
    print(f"Bi  平均耗时: {bi_lat_sum / n:.3f}s | Final 平均耗时: {final_lat_sum / n:.3f}s")

    with open(args.out, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "query",
                "expected_file",
                "bi_rank",
                "bi_hit",
                "final_rank",
                "final_hit",
                "final_top1_file",
                "final_top1_domain",
                "bi_latency_s",
                "final_latency_s",
            ],
        )
        writer.writeheader()
        writer.writerows(out_rows)

    print(f"\n逐条结果已写入：{args.out}")


if __name__ == "__main__":
    main()
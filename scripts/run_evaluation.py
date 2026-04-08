"""Evaluation harness for the MAS Compliance RAG pipeline.

Runs the golden dataset through the full pipeline and a naive (vector-only)
baseline on Category 4 (multi-source) questions, collecting per-question
state, diagnostic metrics, and latency.

Usage:
    uv run python scripts/run_evaluation.py \
        --dataset evaluation/golden_dataset.json \
        --output evaluation/results/evaluation_results_raw.json \
        --include-naive
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import time
import uuid
from pathlib import Path

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI

from msrag.graph import build_context, build_graph


# ---------------------------------------------------------------------------
# Core pipeline runner
# ---------------------------------------------------------------------------


MAX_RETRIES = 5
INITIAL_BACKOFF = 2.0  # seconds


def run_single_query(
    graph, context, query: str, thread_id: str | None = None
) -> dict:
    """Run a single query through the full pipeline, accumulating all node state.

    Retries with exponential backoff on OpenAI rate-limit (429) errors.
    """
    if thread_id is None:
        thread_id = str(uuid.uuid4())

    for attempt in range(MAX_RETRIES):
        try:
            return _run_single_query_inner(graph, context, query, thread_id)
        except Exception as e:
            if "429" in str(e) or "rate_limit" in str(e).lower():
                wait = INITIAL_BACKOFF * (2 ** attempt)
                print(f"    [Rate limited, waiting {wait:.0f}s before retry {attempt + 1}/{MAX_RETRIES}]")
                time.sleep(wait)
            else:
                raise

    # Final attempt without catching
    return _run_single_query_inner(graph, context, query, thread_id)


def _run_single_query_inner(
    graph, context, query: str, thread_id: str
) -> dict:
    """Inner implementation of run_single_query."""
    initial_state = {
        "user_question": query,
        "messages": [HumanMessage(content=query)],
        "retrieval_attempts": 0,
    }
    config = {"configurable": {"thread_id": thread_id}}

    accumulated: dict = {}
    node_sequence: list[str] = []
    start = time.time()

    for event in graph.stream(
        initial_state, config, context=context, stream_mode="updates"
    ):
        for node_name, node_output in event.items():
            node_sequence.append(node_name)
            accumulated.update(node_output)

    elapsed = time.time() - start

    return {
        "final_answer": accumulated.get("final_answer", ""),
        "sources_consulted": accumulated.get("sources_consulted", []),
        "confidence_caveat": accumulated.get("confidence_caveat"),
        "tools_called": accumulated.get("tools_called", []),
        "retrieved_chunks": accumulated.get("retrieved_chunks", []),
        "sql_results": accumulated.get("sql_results", []),
        "web_results": accumulated.get("web_results", []),
        "quality_passed": accumulated.get("quality_passed"),
        "quality_feedback": accumulated.get("quality_feedback"),
        "retrieval_attempts": accumulated.get("retrieval_attempts", 0),
        "agent_reasoning": accumulated.get("final_answer"),
        "node_sequence": node_sequence,
        "latency_seconds": round(elapsed, 2),
    }


# ---------------------------------------------------------------------------
# Naive baseline (vector-only, no agent, no quality gate)
# ---------------------------------------------------------------------------

GENERATE_SYSTEM_PROMPT = """Answer the following question using ONLY the information provided in the context below.

Rules:
- For every factual claim, cite the specific source: [Source: filename] or [SQL Result] or [Web: url].
- If you cannot cite a specific source for a claim, do not include that claim.
- Do NOT invent facts, statistics, regulatory requirements, or penalties not present in the context.
- Do NOT extrapolate beyond what the documents state.
- If the context does not contain enough information to answer, say so explicitly.
"""


def run_naive_baseline(context, query: str) -> dict:
    """Vector-only baseline: direct search + generation, no agent/quality gate."""
    start = time.time()

    results = context.opensearch_client.search(query, mode="hybrid", k=6)

    def sigmoid(x: float) -> float:
        return 1.0 / (1.0 + math.exp(-max(-500, min(500, x))))

    chunks = []
    for doc, logit_score in results:
        chunks.append(
            {
                "content": doc.page_content,
                "score": round(sigmoid(logit_score), 3),
                "metadata": doc.metadata,
            }
        )

    # Format context identically to the generate node
    context_parts = []
    for chunk in chunks:
        source = chunk.get("metadata", {}).get("source_document", "unknown")
        section = chunk.get("metadata", {}).get("section_heading", "")
        label = f"[Source: {source}]"
        if section:
            label += f" (Section: {section})"
        context_parts.append(f"{label}\n{chunk['content']}")

    formatted_context = (
        "\n\n---\n\n".join(context_parts) if context_parts else "No context available."
    )

    llm = ChatOpenAI(model="gpt-5.4-mini", temperature=0.2)
    messages = [
        {"role": "system", "content": GENERATE_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": f"Context:\n{formatted_context}\n\nQuestion: {query}",
        },
    ]

    # Retry on rate limit
    for attempt in range(MAX_RETRIES):
        try:
            response = llm.invoke(messages)
            break
        except Exception as e:
            if "429" in str(e) or "rate_limit" in str(e).lower():
                wait = INITIAL_BACKOFF * (2 ** attempt)
                print(f"    [Naive rate limited, waiting {wait:.0f}s]")
                time.sleep(wait)
            else:
                raise
    else:
        response = llm.invoke(messages)

    elapsed = time.time() - start

    return {
        "final_answer": response.content,
        "retrieved_chunks": chunks,
        "tools_called": ["vector_search"],
        "latency_seconds": round(elapsed, 2),
    }


# ---------------------------------------------------------------------------
# Metric computation
# ---------------------------------------------------------------------------


def classify_failure(result: dict) -> str | None:
    """Classify failure mode for a single result."""
    expected = set(result.get("expected_tools", []))
    actual = set(result.get("tools_called", []))
    has_context = bool(result.get("retrieved_chunks")) or bool(
        result.get("sql_results")
    ) or bool(result.get("web_results"))

    if not expected.issubset(actual):
        return "tool_selection_failure"
    if not has_context:
        return "retrieval_failure"
    # If tools correct and context exists but answer may be wrong -> generation
    # (actual correctness is determined by LLM-as-judge, this is a heuristic)
    return None


def compute_diagnostic_metrics(results: list[dict]) -> dict:
    """Compute aggregate diagnostic metrics from per-question results."""
    total = len(results)
    if total == 0:
        return {}

    # Tool coverage
    tool_hits = sum(
        1
        for r in results
        if set(r.get("expected_tools", [])).issubset(set(r.get("tools_called", [])))
    )

    # Quality gate trigger rate
    gate_triggers = sum(
        1 for r in results if (r.get("retrieval_attempts") or 0) > 1
    )

    # First-attempt tool accuracy
    first_attempt_correct = sum(
        1
        for r in results
        if r.get("node_sequence", []).count("agent_retrieve") == 1
        and set(r.get("expected_tools", [])).issubset(
            set(r.get("tools_called", []))
        )
    )

    # Latency
    latencies = sorted(r["latency_seconds"] for r in results)
    p50 = latencies[int(len(latencies) * 0.5)]
    p95 = latencies[min(int(len(latencies) * 0.95), len(latencies) - 1)]

    # Failure modes
    modes: dict[str, int] = {
        "tool_selection_failure": 0,
        "retrieval_failure": 0,
        "generation_failure": 0,
    }
    for r in results:
        mode = classify_failure(r)
        if mode:
            modes[mode] += 1

    return {
        "total_questions": total,
        "tool_coverage": round(tool_hits / total, 3),
        "quality_gate_trigger_rate": round(gate_triggers / total, 3),
        "first_attempt_tool_accuracy": round(first_attempt_correct / total, 3),
        "latency_p50": p50,
        "latency_p95": p95,
        "latency_mean": round(statistics.mean(latencies), 2),
        "failure_modes": modes,
    }


def compute_category_metrics(results: list[dict]) -> dict:
    """Per-category diagnostic breakdown."""
    by_cat: dict[int, list[dict]] = {}
    for r in results:
        cat = r.get("category", 0)
        by_cat.setdefault(cat, []).append(r)

    output = {}
    for cat, cat_results in sorted(by_cat.items()):
        tool_hits = sum(
            1
            for r in cat_results
            if set(r.get("expected_tools", [])).issubset(
                set(r.get("tools_called", []))
            )
        )
        output[str(cat)] = {
            "count": len(cat_results),
            "tool_coverage": round(tool_hits / len(cat_results), 3),
        }
    return output


# ---------------------------------------------------------------------------
# Main evaluation orchestrator
# ---------------------------------------------------------------------------


def run_evaluation(
    graph,
    context,
    golden_dataset: dict,
    include_naive: bool = False,
) -> dict:
    """Run full evaluation: pipeline + optional naive baseline."""
    questions = golden_dataset["questions"]
    chains = {c["chain_id"]: c for c in golden_dataset.get("multi_turn_chains", [])}

    # Separate standalone vs multi-turn
    standalone = [q for q in questions if q.get("multi_turn_chain_id") is None]
    multi_turn = [q for q in questions if q.get("multi_turn_chain_id") is not None]

    all_results: list[dict] = []

    # --- Standalone questions ---
    print(f"\n=== Running {len(standalone)} standalone questions ===\n")
    for i, q in enumerate(standalone, 1):
        print(f"  [{i}/{len(standalone)}] {q['id']}: {q['query'][:70]}...")
        result = run_single_query(graph, context, q["query"])
        record = {**q, **result}
        record["failure_mode"] = classify_failure(record)
        all_results.append(record)
        print(
            f"    tools={result['tools_called']}, "
            f"latency={result['latency_seconds']}s, "
            f"attempts={result['retrieval_attempts']}"
        )

    # --- Multi-turn chains ---
    chain_ids = sorted(set(q["multi_turn_chain_id"] for q in multi_turn))
    print(f"\n=== Running {len(chain_ids)} multi-turn chains ===\n")
    for chain_id in chain_ids:
        chain_meta = chains.get(chain_id, {})
        print(f"  Chain: {chain_id} — {chain_meta.get('description', '')}")
        thread_id = str(uuid.uuid4())

        turns = sorted(
            [q for q in multi_turn if q["multi_turn_chain_id"] == chain_id],
            key=lambda q: q.get("turn_number", 0),
        )
        for turn in turns:
            print(
                f"    Turn {turn['turn_number']}: {turn['query'][:60]}..."
            )
            result = run_single_query(
                graph, context, turn["query"], thread_id=thread_id
            )
            record = {**turn, **result}
            record["failure_mode"] = classify_failure(record)
            all_results.append(record)
            print(
                f"      tools={result['tools_called']}, "
                f"latency={result['latency_seconds']}s"
            )

    # --- Naive baseline ---
    naive_results: list[dict] = []
    if include_naive:
        naive_candidates = [
            r for r in all_results if r.get("category") == 4
        ]
        print(
            f"\n=== Running naive baseline on {len(naive_candidates)} "
            f"Category 4 (multi-source) questions ===\n"
        )
        for i, r in enumerate(naive_candidates, 1):
            print(f"  [{i}/{len(naive_candidates)}] {r['id']}: {r['query'][:60]}...")
            naive = run_naive_baseline(context, r["query"])
            naive_results.append(
                {
                    "id": r["id"],
                    "query": r["query"],
                    "category": r["category"],
                    "expected_tools": r["expected_tools"],
                    "ground_truth_answer": r["ground_truth_answer"],
                    **naive,
                }
            )
            print(f"    latency={naive['latency_seconds']}s")

    # --- Compute metrics ---
    diagnostics = compute_diagnostic_metrics(all_results)
    by_category = compute_category_metrics(all_results)

    return {
        "run_metadata": {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "pipeline_model": "gpt-5.4-mini",
            "judge_model": "gpt-5.4-mini",
            "golden_dataset_version": golden_dataset.get("version", "unknown"),
            "total_questions": len(all_results),
            "total_standalone": len(standalone),
            "total_multi_turn_chains": len(chain_ids),
            "include_naive": include_naive,
        },
        "diagnostic_metrics": diagnostics,
        "by_category": by_category,
        "per_question_results": _strip_large_fields(all_results),
        "naive_baseline_results": _strip_large_fields(naive_results),
    }


def _strip_large_fields(results: list[dict]) -> list[dict]:
    """Remove bulky retrieval content from serialised output to keep file manageable.

    Keeps a summary (count + truncated content) instead of full chunks/rows.
    """
    stripped = []
    for r in results:
        r2 = dict(r)

        # Summarise retrieved_chunks
        chunks = r2.pop("retrieved_chunks", []) or []
        r2["retrieved_chunk_count"] = len(chunks)
        r2["retrieved_chunk_sources"] = list(
            {c.get("metadata", {}).get("source_document", "?") for c in chunks}
        )

        # Summarise sql_results
        sql = r2.pop("sql_results", []) or []
        r2["sql_result_count"] = len(sql)

        # Summarise web_results
        web = r2.pop("web_results", []) or []
        r2["web_result_count"] = len(web)

        # Drop messages (very large, not needed for scoring)
        r2.pop("messages", None)

        stripped.append(r2)
    return stripped


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="Run RAG evaluation harness")
    parser.add_argument(
        "--dataset",
        default="evaluation/golden_dataset.json",
        help="Path to golden dataset JSON",
    )
    parser.add_argument(
        "--output",
        default="evaluation/results/evaluation_results_raw.json",
        help="Output path for raw results",
    )
    parser.add_argument(
        "--include-naive",
        action="store_true",
        help="Run naive vector-only baseline on Category 4 (multi-source) questions",
    )
    args = parser.parse_args()

    load_dotenv()

    # Load golden dataset
    dataset_path = Path(args.dataset)
    if not dataset_path.exists():
        print(f"Error: dataset not found at {dataset_path}")
        return
    golden = json.loads(dataset_path.read_text(encoding="utf-8"))
    print(
        f"Loaded {len(golden['questions'])} questions, "
        f"{len(golden.get('multi_turn_chains', []))} chains"
    )

    # Initialise pipeline
    print("\nInitialising pipeline...")
    context = build_context()
    graph = build_graph()

    # Run evaluation
    results = run_evaluation(graph, context, golden, include_naive=args.include_naive)

    # Save results
    output_path = Path(args.output)
    output_path.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    print(f"\nResults saved to {output_path}")

    # Print summary
    diag = results["diagnostic_metrics"]
    print("\n" + "=" * 60)
    print("DIAGNOSTIC SUMMARY")
    print("=" * 60)
    print(f"  Total questions:             {diag.get('total_questions', 0)}")
    print(f"  Tool coverage:               {diag.get('tool_coverage', 0):.1%}")
    print(f"  Quality gate trigger rate:   {diag.get('quality_gate_trigger_rate', 0):.1%}")
    print(f"  First-attempt tool accuracy: {diag.get('first_attempt_tool_accuracy', 0):.1%}")
    print(f"  Latency P50:                 {diag.get('latency_p50', 0):.1f}s")
    print(f"  Latency P95:                 {diag.get('latency_p95', 0):.1f}s")
    print(f"  Failure modes:               {diag.get('failure_modes', {})}")
    print()

    cats = results.get("by_category", {})
    for cat, m in sorted(cats.items()):
        print(f"  Category {cat}: {m['count']} questions, tool_coverage={m['tool_coverage']:.1%}")

    if results.get("naive_baseline_results"):
        print(
            f"\n  Naive baseline: {len(results['naive_baseline_results'])} questions "
            f"(scoring deferred to evaluate_judges.py)"
        )


if __name__ == "__main__":
    main()

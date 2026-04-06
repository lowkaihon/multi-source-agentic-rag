"""LLM-as-judge scoring for RAG evaluation results.

Reads raw results from run_evaluation.py, scores each question with three
judges (correctness, completeness, groundedness) using gpt-5.4-mini, scores
Category 4 naive baseline results, and writes enriched results with
aggregate metrics.

Usage:
    uv run python scripts/evaluate_judges.py \
        --input evaluation/results/evaluation_results_raw.json \
        --output evaluation/results/evaluation_results.json
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI


# ---------------------------------------------------------------------------
# Judge prompts
# ---------------------------------------------------------------------------

CORRECTNESS_PROMPT = """You are evaluating whether a RAG system's answer correctly addresses the question.

Question: {question}

Ground Truth Answer: {ground_truth}

System Answer: {system_answer}

Rate the system answer on a scale of 1-5:
1 - Completely incorrect or irrelevant
2 - Partially addresses the question but contains major factual errors
3 - Addresses the question but misses important points or contains minor errors
4 - Mostly correct with only minor omissions
5 - Fully correct and comprehensive

Respond with ONLY a JSON object:
{{"score": <1-5>, "reasoning": "<brief explanation>"}}"""

COMPLETENESS_PROMPT = """You are evaluating whether a RAG system's answer covers all required aspects of a multi-source question.

Question: {question}

Required aspects that should be covered:
{aspects_list}

System Answer: {system_answer}

For each aspect, indicate whether it is covered (true/false) and provide a brief explanation.

Respond with ONLY a JSON object:
{{"aspects": [{{"aspect": "<aspect text>", "covered": true, "explanation": "<brief>"}}], "completeness_score": <0.0-1.0>}}"""

GROUNDEDNESS_PROMPT = """You are evaluating whether a RAG system's answer is grounded in the provided context — i.e., every factual claim is supported by the retrieved information.

Retrieved Context Summary:
{context}

System Answer: {system_answer}

Identify the key factual claims in the answer. For each claim, determine:
- SUPPORTED: directly supported by the context
- PARTIALLY_SUPPORTED: related information exists but the specific claim is inferred
- UNSUPPORTED: no supporting evidence in the context (potential hallucination)

Respond with ONLY a JSON object:
{{"claims": [{{"claim": "<text>", "verdict": "SUPPORTED", "evidence": "<brief>"}}], "groundedness_score": <0.0-1.0>}}"""

NAIVE_CORRECTNESS_PROMPT = """You are evaluating whether a naive (vector-search only) RAG baseline produced a correct and complete answer for a multi-source question that ideally requires both document retrieval and structured data.

Question: {question}

Ground Truth Answer: {ground_truth}

Naive Baseline Answer (vector-search only, no SQL or agent routing):
{naive_answer}

Rate the naive answer on a scale of 1-5:
1 - Completely incorrect or irrelevant — missed the core information entirely
2 - Partially addresses the question but critically incomplete — key aspects that require non-vector sources are missing
3 - Addresses some aspects from document retrieval but missing important quantitative, structured, or cross-referenced information
4 - Mostly correct — vector search alone covered most of the answer
5 - Fully correct — multi-source retrieval would not have improved this answer

Respond with ONLY a JSON object:
{{"score": <1-5>, "reasoning": "<brief explanation of what was missing or what multi-source retrieval would have added>"}}"""


# ---------------------------------------------------------------------------
# Judge execution
# ---------------------------------------------------------------------------


def _call_judge(llm: ChatOpenAI, prompt: str) -> dict:
    """Call the LLM judge and parse JSON response."""
    response = llm.invoke([{"role": "user", "content": prompt}])
    text = response.content.strip()

    # Strip markdown code fences if present
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:])
        if text.endswith("```"):
            text = text[:-3].strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"error": "Failed to parse judge response", "raw": text}


def judge_correctness(llm: ChatOpenAI, result: dict) -> dict:
    """Score answer correctness against ground truth."""
    prompt = CORRECTNESS_PROMPT.format(
        question=result["query"],
        ground_truth=result.get("ground_truth_answer", ""),
        system_answer=result.get("final_answer", ""),
    )
    return _call_judge(llm, prompt)


def judge_completeness(llm: ChatOpenAI, result: dict) -> dict | None:
    """Score answer completeness for multi-aspect questions."""
    aspects = result.get("ground_truth_aspects", [])
    if not aspects:
        return None

    aspects_text = "\n".join(f"- {a}" for a in aspects)
    prompt = COMPLETENESS_PROMPT.format(
        question=result["query"],
        aspects_list=aspects_text,
        system_answer=result.get("final_answer", ""),
    )
    return _call_judge(llm, prompt)


def judge_groundedness(llm: ChatOpenAI, result: dict) -> dict:
    """Score whether claims are grounded in retrieved context."""
    # Reconstruct context summary from result metadata
    context_parts = []

    chunk_count = result.get("retrieved_chunk_count", 0)
    chunk_sources = result.get("retrieved_chunk_sources", [])
    if chunk_count > 0:
        context_parts.append(
            f"Vector search returned {chunk_count} chunks from: "
            + ", ".join(chunk_sources)
        )

    sql_count = result.get("sql_result_count", 0)
    if sql_count > 0:
        context_parts.append(f"SQL query returned {sql_count} rows")

    web_count = result.get("web_result_count", 0)
    if web_count > 0:
        context_parts.append(f"Web search returned {web_count} results")

    if not context_parts:
        context_parts.append("No context was retrieved")

    context_text = "\n".join(context_parts)

    # Also include citations as evidence of what was available
    citations = result.get("citations", [])
    if citations:
        sources = [c.get("source", "") for c in citations]
        context_text += f"\nCitations in answer: {', '.join(sources)}"

    prompt = GROUNDEDNESS_PROMPT.format(
        context=context_text,
        system_answer=result.get("final_answer", ""),
    )
    return _call_judge(llm, prompt)


def judge_naive_correctness(llm: ChatOpenAI, naive_result: dict) -> dict:
    """Score naive baseline answer against ground truth."""
    prompt = NAIVE_CORRECTNESS_PROMPT.format(
        question=naive_result["query"],
        ground_truth=naive_result.get("ground_truth_answer", ""),
        naive_answer=naive_result.get("final_answer", ""),
    )
    return _call_judge(llm, prompt)


# ---------------------------------------------------------------------------
# Aggregate scoring
# ---------------------------------------------------------------------------


def compute_aggregate_scores(scored_results: list[dict]) -> dict:
    """Compute aggregate primary metrics from scored results."""
    correctness_scores = [
        r["correctness"]["score"]
        for r in scored_results
        if r.get("correctness") and "score" in r["correctness"]
    ]

    completeness_scores = [
        r["completeness"]["completeness_score"]
        for r in scored_results
        if r.get("completeness") and "completeness_score" in r["completeness"]
    ]

    groundedness_scores = [
        r["groundedness"]["groundedness_score"]
        for r in scored_results
        if r.get("groundedness") and "groundedness_score" in r["groundedness"]
    ]

    return {
        "answer_correctness_mean": (
            round(statistics.mean(correctness_scores), 2)
            if correctness_scores
            else None
        ),
        "answer_correctness_median": (
            round(statistics.median(correctness_scores), 1)
            if correctness_scores
            else None
        ),
        "answer_completeness_mean": (
            round(statistics.mean(completeness_scores), 3)
            if completeness_scores
            else None
        ),
        "groundedness_mean": (
            round(statistics.mean(groundedness_scores), 3)
            if groundedness_scores
            else None
        ),
        "correctness_distribution": {
            str(i): sum(1 for s in correctness_scores if s == i) for i in range(1, 6)
        },
        "total_scored": len(correctness_scores),
    }


def compute_category_scores(scored_results: list[dict]) -> dict:
    """Per-category judge score breakdown."""
    by_cat: dict[int, list[dict]] = {}
    for r in scored_results:
        cat = r.get("category", 0)
        by_cat.setdefault(cat, []).append(r)

    output = {}
    for cat, results in sorted(by_cat.items()):
        scores = [
            r["correctness"]["score"]
            for r in results
            if r.get("correctness") and "score" in r["correctness"]
        ]
        output[str(cat)] = {
            "count": len(results),
            "correctness_mean": (
                round(statistics.mean(scores), 2) if scores else None
            ),
        }

        # Completeness for category 4
        comp = [
            r["completeness"]["completeness_score"]
            for r in results
            if r.get("completeness") and "completeness_score" in r["completeness"]
        ]
        if comp:
            output[str(cat)]["completeness_mean"] = round(statistics.mean(comp), 3)

    return output


def compute_naive_comparison(naive_scored: list[dict]) -> dict:
    """Compute naive baseline failure rate."""
    if not naive_scored:
        return {}

    scores = [
        r["naive_correctness"]["score"]
        for r in naive_scored
        if r.get("naive_correctness") and "score" in r["naive_correctness"]
    ]
    if not scores:
        return {}

    # "Failure" = score <= 2 (completely incorrect or critically missing data)
    failures = sum(1 for s in scores if s <= 2)

    by_cat: dict[int, list[int]] = {}
    for r in naive_scored:
        if r.get("naive_correctness") and "score" in r["naive_correctness"]:
            cat = r.get("category", 0)
            by_cat.setdefault(cat, []).append(r["naive_correctness"]["score"])

    result = {
        "total_evaluated": len(scores),
        "failure_count": failures,
        "failure_rate_combined": round(failures / len(scores), 3),
        "correctness_mean": round(statistics.mean(scores), 2),
    }

    for cat, cat_scores in sorted(by_cat.items()):
        cat_failures = sum(1 for s in cat_scores if s <= 2)
        result[f"failure_rate_category_{cat}"] = round(
            cat_failures / len(cat_scores), 3
        )

    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="LLM-as-judge scoring")
    parser.add_argument(
        "--input",
        default="evaluation/results/evaluation_results_raw.json",
        help="Raw results from run_evaluation.py",
    )
    parser.add_argument(
        "--output",
        default="evaluation/results/evaluation_results.json",
        help="Output path for scored results",
    )
    args = parser.parse_args()

    load_dotenv()

    # Load raw results
    input_path = Path(args.input)
    if not input_path.exists():
        print(f"Error: input not found at {input_path}")
        return
    raw = json.loads(input_path.read_text(encoding="utf-8"))

    results = raw["per_question_results"]
    naive_results = raw.get("naive_baseline_results", [])

    print(f"Loaded {len(results)} results, {len(naive_results)} naive baseline results")

    # Initialise judge (gpt-5.4-mini)
    judge_llm = ChatOpenAI(model="gpt-5.4-mini", temperature=0)

    # --- Score pipeline results ---
    print(f"\n=== Scoring {len(results)} pipeline results ===\n")
    scored_results = []
    for i, r in enumerate(results, 1):
        print(f"  [{i}/{len(results)}] {r['id']}: ", end="", flush=True)

        correctness = judge_correctness(judge_llm, r)
        print(f"correctness={correctness.get('score', '?')}", end="", flush=True)

        completeness = judge_completeness(judge_llm, r)
        if completeness:
            print(
                f", completeness={completeness.get('completeness_score', '?')}",
                end="",
                flush=True,
            )

        groundedness = judge_groundedness(judge_llm, r)
        print(
            f", groundedness={groundedness.get('groundedness_score', '?')}",
            flush=True,
        )

        scored_results.append(
            {
                **r,
                "correctness": correctness,
                "completeness": completeness,
                "groundedness": groundedness,
            }
        )

    # --- Score naive baseline ---
    naive_scored = []
    if naive_results:
        print(f"\n=== Scoring {len(naive_results)} naive baseline results ===\n")
        for i, nr in enumerate(naive_results, 1):
            print(f"  [{i}/{len(naive_results)}] {nr['id']}: ", end="", flush=True)
            nc = judge_naive_correctness(judge_llm, nr)
            print(f"score={nc.get('score', '?')}", flush=True)
            naive_scored.append({**nr, "naive_correctness": nc})

    # --- Aggregate ---
    primary_scores = compute_aggregate_scores(scored_results)
    category_scores = compute_category_scores(scored_results)
    naive_comparison = compute_naive_comparison(naive_scored)

    output = {
        "run_metadata": {
            **raw.get("run_metadata", {}),
            "judge_timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        },
        "aggregate_metrics": {
            "primary": primary_scores,
            "diagnostic": raw.get("diagnostic_metrics", {}),
            "by_category": category_scores,
            "naive_baseline": naive_comparison,
        },
        "per_question_results": scored_results,
        "naive_baseline_results": naive_scored,
    }

    # Save
    output_path = Path(args.output)
    output_path.write_text(json.dumps(output, indent=2, default=str), encoding="utf-8")
    print(f"\nScored results saved to {output_path}")

    # Print summary
    print("\n" + "=" * 60)
    print("EVALUATION SUMMARY")
    print("=" * 60)

    p = primary_scores
    print(f"  Correctness (mean):    {p.get('answer_correctness_mean', 'N/A')}/5.0")
    print(f"  Correctness (median):  {p.get('answer_correctness_median', 'N/A')}/5.0")
    print(f"  Completeness (mean):   {p.get('answer_completeness_mean', 'N/A')}")
    print(f"  Groundedness (mean):   {p.get('groundedness_mean', 'N/A')}")
    print(f"  Score distribution:    {p.get('correctness_distribution', {})}")
    print()

    for cat, m in sorted(category_scores.items()):
        parts = [f"correctness={m.get('correctness_mean', 'N/A')}"]
        if "completeness_mean" in m:
            parts.append(f"completeness={m['completeness_mean']}")
        print(f"  Category {cat} ({m['count']}): {', '.join(parts)}")

    if naive_comparison:
        print(f"\n  Naive baseline failure rate: {naive_comparison.get('failure_rate_combined', 'N/A'):.1%}")
        for k, v in naive_comparison.items():
            if k.startswith("failure_rate_category_"):
                cat = k.replace("failure_rate_category_", "")
                print(f"    Category {cat}: {v:.1%}")

    # Diagnostic reminder
    diag = raw.get("diagnostic_metrics", {})
    print(f"\n  Tool coverage:               {diag.get('tool_coverage', 'N/A')}")
    print(f"  Quality gate trigger rate:   {diag.get('quality_gate_trigger_rate', 'N/A')}")
    print(f"  First-attempt tool accuracy: {diag.get('first_attempt_tool_accuracy', 'N/A')}")
    print(f"  Latency P50/P95:             {diag.get('latency_p50', '?')}s / {diag.get('latency_p95', '?')}s")


if __name__ == "__main__":
    main()

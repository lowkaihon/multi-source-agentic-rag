"""Generate node: grounded answer generation with citations."""

from __future__ import annotations

import json
import re

from langchain_core.runnables import RunnableConfig
from langchain_openai import ChatOpenAI

from msrag.state import PipelineState


def _extract_citations(answer_text: str, state: PipelineState) -> list[dict]:
    """Extract citations from the generated answer text.

    Recognizes patterns: [Source: filename], [SQL Result], [Web: url]
    """
    citations = []
    seen = set()

    # [Source: filename] or [Source: filename, page X]
    for match in re.finditer(r"\[Source:\s*([^\]]+)\]", answer_text):
        ref = match.group(1).strip()
        if ref not in seen:
            seen.add(ref)
            citations.append({"type": "document", "source": ref})

    # [SQL Result]
    if "[SQL Result]" in answer_text:
        citations.append({"type": "sql", "source": "structured database"})

    # [Web: url]
    for match in re.finditer(r"\[Web:\s*([^\]]+)\]", answer_text):
        url = match.group(1).strip()
        if url not in seen:
            seen.add(url)
            citations.append({"type": "web", "source": url})

    return citations


def generate_node(state: PipelineState, config: RunnableConfig) -> dict:
    """Assemble context from all sources and generate a grounded answer."""
    quality_passed = state.get("quality_passed", True)
    attempts = state.get("retrieval_attempts", 0)

    # Assemble context from all sources with source labels
    context_parts = []

    for chunk in state.get("retrieved_chunks") or []:
        source = chunk.get("metadata", {}).get("source_document", "unknown")
        section = chunk.get("metadata", {}).get("section_heading", "")
        label = f"[Source: {source}]"
        if section:
            label += f" (Section: {section})"
        context_parts.append(f"{label}\n{chunk['content']}")

    for row in state.get("sql_results") or []:
        context_parts.append(f"[SQL Result]\n{json.dumps(row, default=str)}")

    for result in state.get("web_results") or []:
        url = result.get("url", "unknown")
        context_parts.append(f"[Web: {url}]\n{result.get('snippet', '')}")

    formatted_context = (
        "\n\n---\n\n".join(context_parts)
        if context_parts
        else "No context available."
    )

    # Quality note for incomplete retrieval
    if quality_passed:
        quality_note = ""
    else:
        quality_note = (
            "\nIMPORTANT: The retrieved context may be incomplete. "
            "If information is insufficient to answer fully, explicitly state what is missing "
            "rather than filling gaps from general knowledge.\n"
        )

    system_prompt = f"""Answer the following question using ONLY the information provided in the context below.
{quality_note}
Rules:
- For every factual claim, cite the specific source: [Source: filename] or [SQL Result] or [Web: url].
- If you cannot cite a specific source for a claim, do not include that claim.
- Do NOT invent facts, statistics, regulatory requirements, or penalties not present in the context.
- Do NOT extrapolate beyond what the documents state.
- If the context does not contain enough information to answer, say so explicitly.
"""

    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0.2)
    response = llm.invoke(
        [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": f"Context:\n{formatted_context}\n\nQuestion: {state['user_question']}",
            },
        ]
    )

    confidence_caveat = None
    if not quality_passed and attempts >= 2:
        confidence_caveat = (
            "Note: This answer is based on limited context. "
            "The retrieved documents may not fully cover this topic. "
            "Please verify against the original MAS publications."
        )

    return {
        "final_answer": response.content,
        "confidence_caveat": confidence_caveat,
        "citations": _extract_citations(response.content, state),
    }

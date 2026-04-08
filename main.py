"""CLI REPL entry point for the Multi-Source RAG pipeline."""

import uuid

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage

from msrag.graph import build_context, build_graph


def main():
    load_dotenv()

    print("Initializing pipeline...")
    context = build_context(
        manifest_path="corpus/ingestion_output/metadata_manifest.json",
        ddl_path="corpus/data/sql/init_schema.sql",
    )
    graph = build_graph()

    # Single thread_id for entire REPL session — enables multi-turn.
    # MemorySaver persists state, add_messages accumulates history,
    # agent resolves coreferences from prior turns.
    thread_id = str(uuid.uuid4())
    config = {"configurable": {"thread_id": thread_id}}

    print("\nMAS Compliance RAG (type 'quit' to exit)")
    print("=" * 50)

    while True:
        try:
            query = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye!")
            break

        if query.lower() in ("quit", "exit", "q"):
            break
        if not query:
            continue

        initial_state = {
            "user_question": query,
            "messages": [HumanMessage(content=query)],
            "retrieval_attempts": 0,
        }

        # Stream with node-level progress output
        final_state = {}
        for event in graph.stream(
            initial_state, config, context=context, stream_mode="updates"
        ):
            for node_name, node_output in event.items():
                final_state.update(node_output)
                if node_name == "agent_retrieve":
                    tools_used = node_output.get("tools_called", [])
                    print(f"  [Retrieving... tools: {', '.join(tools_used) if tools_used else 'none'}]")
                elif node_name == "quality_gate":
                    passed = node_output.get("quality_passed")
                    if not passed and node_output.get("retrieval_attempts", 0) < 2:
                        feedback = node_output.get("quality_feedback", "")
                        print(f"  [Quality gate: retry — {feedback}]")
                    else:
                        print("  [Quality gate: passed]")

        if final_state.get("final_answer"):
            print(f"\n{final_state['final_answer']}")
            if final_state.get("confidence_caveat"):
                print(f"\n{final_state['confidence_caveat']}")
            if final_state.get("sources_consulted"):
                sources = [c.get("source", "") for c in final_state["sources_consulted"]]
                print(f"\nSources: {', '.join(sources)}")


if __name__ == "__main__":
    main()

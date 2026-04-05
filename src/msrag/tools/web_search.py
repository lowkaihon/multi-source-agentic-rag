"""Web search wrapper using Tavily API."""

from __future__ import annotations

import os


def search_web(query: str, max_results: int = 5) -> list[dict]:
    """Search the web via Tavily. Returns empty list if no API key or on failure."""
    if not os.environ.get("TAVILY_API_KEY"):
        return []

    try:
        from tavily import TavilyClient

        client = TavilyClient()
        response = client.search(
            query=query,
            search_depth="advanced",
            max_results=max_results,
            include_raw_content=False,
        )
        return [
            {
                "snippet": r.get("content", ""),
                "url": r.get("url", ""),
                "title": r.get("title", ""),
            }
            for r in response.get("results", [])
        ]
    except Exception:
        return []

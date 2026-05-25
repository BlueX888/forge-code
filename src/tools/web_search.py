"""WebSearch tool using DuckDuckGo Search."""

from __future__ import annotations

from typing import Any

from main.config import AgentConfig
from safety.permissions import SafetyLabel
from tools.base import ToolResult
from tools.names import ToolName


class WebSearchTool:
    """Search the web using DuckDuckGo."""

    @property
    def name(self) -> str:
        return ToolName.WEB_SEARCH

    @property
    def description(self) -> str:
        return (
            "Search the web using DuckDuckGo and return a list of results "
            "with titles, summaries (snippets), and URLs."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query keywords",
                },
                "num_results": {
                    "type": "integer",
                    "description": "Number of results to return (default 5, max 20)",
                    "default": 5,
                },
                "region": {
                    "type": "string",
                    "description": "Region code for the search (e.g. 'us-en', 'cn-zh', 'wt-wt')",
                },
            },
            "required": ["query"],
        }

    @property
    def safety_label(self) -> SafetyLabel:
        return SafetyLabel.READONLY

    def run(self, *, arguments: dict[str, Any], config: AgentConfig) -> ToolResult:
        query = arguments.get("query", "").strip()
        if not query:
            return ToolResult(False, "", "Search query cannot be empty.")

        # Default results to 5, clamp max_results between 1 and 20
        num_results = arguments.get("num_results", 5)
        try:
            num_results = int(num_results)
        except (ValueError, TypeError):
            num_results = 5
        num_results = max(1, min(num_results, 20))

        region = arguments.get("region")
        if region and not isinstance(region, str):
            region = str(region)
        region = region.strip() if region else None

        try:
            from duckduckgo_search import DDGS
        except ImportError:
            return ToolResult(
                False,
                "",
                "The 'duckduckgo_search' package is not installed. Please install it with: pip install duckduckgo-search",
            )

        try:
            kwargs: dict[str, Any] = {"max_results": num_results}
            if region:
                kwargs["region"] = region

            with DDGS() as ddgs:
                results = list(ddgs.text(query, **kwargs))

            if not results:
                return ToolResult(True, f"No search results found for query: '{query}'")

            formatted_results = []
            for i, res in enumerate(results, 1):
                title = res.get("title", "No Title")
                url = res.get("href", res.get("url", "No URL"))
                snippet = res.get("body", res.get("snippet", ""))
                formatted_results.append(
                    f"{i}. Title: {title}\n"
                    f"   URL: {url}\n"
                    f"   Snippet: {snippet}"
                )

            output = f"Search results for '{query}':\n\n" + "\n\n".join(formatted_results)
            return ToolResult(True, output)

        except Exception as exc:
            return ToolResult(False, "", f"Error searching the web: {exc}")

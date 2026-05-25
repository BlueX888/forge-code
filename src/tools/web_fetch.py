"""WebFetch tool to fetch a URL and extract text/HTML contents."""

from __future__ import annotations

import html.parser
from typing import Any

from main.config import AgentConfig
from safety.permissions import SafetyLabel
from tools.base import ToolResult
from tools.names import ToolName


class _HTMLTextExtractor(html.parser.HTMLParser):
    """Extends HTMLParser to extract readable plain text from HTML, skipping script/style/head tags."""

    def __init__(self) -> None:
        super().__init__()
        self.text_parts: list[str] = []
        self.ignore_stack: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag_lower = tag.lower()
        if tag_lower in ("script", "style", "head", "meta", "link"):
            self.ignore_stack.append(tag_lower)
        elif tag_lower in ("p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "tr", "br"):
            self.text_parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag_lower = tag.lower()
        if tag_lower in ("script", "style", "head", "meta", "link"):
            if self.ignore_stack and self.ignore_stack[-1] == tag_lower:
                self.ignore_stack.pop()
        elif tag_lower in ("p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "tr"):
            self.text_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.ignore_stack:
            self.text_parts.append(data)

    def get_text(self) -> str:
        raw_text = "".join(self.text_parts)
        lines = []
        for line in raw_text.splitlines():
            cleaned = " ".join(line.split())
            if cleaned:
                lines.append(cleaned)
        return "\n".join(lines)


class WebFetchTool:
    """Fetch content of a URL and extract clean plain text."""

    @property
    def name(self) -> str:
        return ToolName.WEB_FETCH

    @property
    def description(self) -> str:
        return (
            "Fetch the contents of a URL and convert it to plain text. "
            "If the page is HTML, it will parse and extract readable text, ignoring script and style tags."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The URL of the webpage to fetch",
                },
                "max_length": {
                    "type": "integer",
                    "description": "Maximum number of characters of text to return (default 10000)",
                    "default": 10000,
                },
            },
            "required": ["url"],
        }

    @property
    def safety_label(self) -> SafetyLabel:
        return SafetyLabel.READONLY

    def run(self, *, arguments: dict[str, Any], config: AgentConfig) -> ToolResult:
        url = arguments.get("url", "").strip()
        if not url:
            return ToolResult(False, "", "URL cannot be empty.")

        if not (url.startswith("http://") or url.startswith("https://")):
            # Prepend https if schema is missing
            url = "https://" + url

        max_length = arguments.get("max_length", 10000)
        try:
            max_length = int(max_length)
        except (ValueError, TypeError):
            max_length = 10000
        max_length = max(100, max_length)

        try:
            import httpx
        except ImportError:
            return ToolResult(
                False,
                "",
                "The 'httpx' package is not installed. Please install it with: pip install httpx",
            )

        try:
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,text/plain,application/json,application/xhtml+xml,*/*",
            }
            with httpx.Client(timeout=15.0, follow_redirects=True, headers=headers) as client:
                response = client.get(url)

            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            return ToolResult(
                False,
                "",
                f"HTTP status error {exc.response.status_code} occurred while fetching {url}: {exc}",
            )
        except httpx.HTTPError as exc:
            return ToolResult(
                False,
                "",
                f"HTTP error occurred while fetching {url}: {exc}",
            )
        except Exception as exc:
            return ToolResult(
                False,
                "",
                f"Failed to fetch {url}: {exc}",
            )

        content_type = response.headers.get("Content-Type", "").lower()

        # Extract text based on Content-Type
        if "text/html" in content_type or "application/xhtml+xml" in content_type:
            try:
                # Use raw bytes and response.text's encoding standard to decode safely
                html_content = response.text
                parser = _HTMLTextExtractor()
                parser.feed(html_content)
                text = parser.get_text()
            except Exception as exc:
                return ToolResult(
                    False,
                    "",
                    f"Failed to parse HTML from {url}: {exc}",
                )
        elif (
            "text/plain" in content_type
            or "application/json" in content_type
            or "text/markdown" in content_type
        ):
            text = response.text
        else:
            return ToolResult(
                False,
                "",
                f"Unsupported content-type: '{content_type}'. Only HTML, plain text, JSON, or Markdown are supported.",
            )

        if len(text) > max_length:
            text = text[:max_length] + f"\n\n[Content truncated to {max_length} characters]"

        return ToolResult(True, text)

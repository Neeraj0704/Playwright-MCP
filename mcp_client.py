from __future__ import annotations

import os
import json
import shlex
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


@dataclass
class MCPConfig:
    cmd: str
    args: List[str]


def _get_config_from_env() -> Optional[MCPConfig]:
    """
    Read how to start the Playwright MCP Server from env:
      MCP_PLAYWRIGHT_CMD  (required)
      MCP_PLAYWRIGHT_ARGS (optional, space-separated)

    Example:
      export MCP_PLAYWRIGHT_CMD="python3"
      export MCP_PLAYWRIGHT_ARGS="simple_mcp_server.py"
    """
    cmd = os.getenv("MCP_PLAYWRIGHT_CMD")
    if not cmd:
        return None
    args = shlex.split(os.getenv("MCP_PLAYWRIGHT_ARGS", ""))
    return MCPConfig(cmd=cmd, args=args)


class PlaywrightMCP:
    """
    Minimal MCP client wrapper. Starts/attaches to a Playwright MCP Server
    via stdio, lists tools, and lets you call them.
    """

    def __init__(self, cfg: MCPConfig):
        self.cfg = cfg
        self._read = None
        self._write = None
        self._session: Optional[ClientSession] = None
        self._tools: Dict[str, Any] = {}
        self._stdio_ctx = None

    async def __aenter__(self) -> "PlaywrightMCP":
        # Create server parameters with command and args
        server_params = StdioServerParameters(
            command=self.cfg.cmd,
            args=self.cfg.args,
            env=None
        )
        
        print(f"[MCP CLIENT] Starting: {self.cfg.cmd} {' '.join(self.cfg.args)}")
        
        # Get read/write streams via context manager
        self._stdio_ctx = stdio_client(server_params)
        self._read, self._write = await self._stdio_ctx.__aenter__()
        
        # Create and initialize session
        self._session = ClientSession(self._read, self._write)
        await self._session.__aenter__()
        await self._session.initialize()
        
        print("[MCP CLIENT] ✅ Session initialized")
        
        await self._refresh_tools()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self._session:
            try:
                await self._session.__aexit__(exc_type, exc, tb)
            except Exception:
                pass
        if self._stdio_ctx:
            try:
                await self._stdio_ctx.__aexit__(exc_type, exc, tb)
            except Exception:
                pass

    async def _refresh_tools(self):
        assert self._session
        result = await self._session.list_tools()
        self._tools = {t.name: t for t in result.tools}
        print(f"[MCP CLIENT] Found {len(self._tools)} tools: {list(self._tools.keys())}")

    def list_tool_names(self) -> List[str]:
        return list(self._tools.keys())

    def _match_tool(self, candidates: List[str]) -> Optional[str]:
        """
        Find a tool whose name starts with OR contains any candidate (case-insensitive).
        Prefer startswith; fall back to contains.
        """
        names = self.list_tool_names()
        low_names = [n.lower() for n in names]
        for c in candidates:
            c = c.lower()
            # prefer startswith
            for i, ln in enumerate(low_names):
                if ln.startswith(c):
                    return names[i]
            # fallback to contains
            for i, ln in enumerate(low_names):
                if c in ln:
                    return names[i]
        return None

    async def call(self, tool_name: str, args: Dict[str, Any]) -> Any:
        """
        Call an MCP tool by name with args. Returns the first JSON-ish result.
        """
        assert self._session
        result = await self._session.call_tool(tool_name, args)
        
        # Tool results can be a list of "content" items; try to JSON-parse
        if not result or not result.content:
            return None
        
        for item in result.content:
            # Access attributes directly (not as dict)
            val = getattr(item, 'text', None)
            if val is None:
                continue
            try:
                return json.loads(val)
            except Exception:
                return val
        return None

    async def navigate(self, url: str) -> bool:
        """
        Ask the server to navigate its browser/page to a URL.
        We try a few common tool name prefixes.
        """
        name = self._match_tool([
            "playwright_navigate",
            "playwright.navigate",
            "page.navigate",
            "navigate",
        ])
        if not name:
            print(f"[MCP CLIENT] ⚠️  No navigate tool found")
            return False

        print(f"[MCP CLIENT] Calling {name} with url={url}")
        res = await self.call(name, {"url": url})
        return True  # Many servers return nothing on success

    async def get_page_context(self) -> Optional[Dict[str, Any]]:
        """
        Ask the server for a compact page context (roles/names/selectors).
        We try a few common tool name prefixes and normalize the response.
        """
        name = self._match_tool([
            "playwright_snapshot",
            "playwright.snapshot",
            "page.snapshot",
            "snapshot",
        ])
        if not name:
            print(f"[MCP CLIENT] ⚠️  No snapshot tool found")
            return None

        print(f"[MCP CLIENT] Calling {name}")
        data = await self.call(name, {})
        if not data:
            print("[MCP CLIENT] ⚠️  Snapshot returned no data")
            return None

        print(f"[MCP CLIENT] Got snapshot with keys: {list(data.keys())}")

        # Normalize to our expected structure
        ctx: Dict[str, Any] = {"url": "", "elements": [], "testids": [], "hints": {}}

        ctx["url"] = data.get("url", "")
        ctx["title"] = data.get("title", "")
        
        # Extract elements
        elements = data.get("elements", [])
        ctx["elements"] = elements
        
        print(f"[MCP CLIENT] ✅ Extracted {len(elements)} elements")
        
        return ctx


def build_playwright_mcp() -> Optional[PlaywrightMCP]:
    cfg = _get_config_from_env()
    if not cfg:
        return None
    return PlaywrightMCP(cfg)
#!/usr/bin/env python3
"""
Simple MCP server that provides Playwright browser automation tools.
Run with: python simple_mcp_server.py
"""

import asyncio
import json
import os
import sys
import traceback
from typing import Any, Optional

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent
from playwright.async_api import async_playwright, Browser, Page


def log(msg: str):
    """Log to stderr so it doesn't interfere with JSON-RPC on stdout."""
    print(msg, file=sys.stderr, flush=True)


# Set MCP_HEADLESS=0 to see the MCP browser; default is headless.
HEADLESS = os.getenv("MCP_HEADLESS", "1") not in {"0", "false", "False"}


class PlaywrightMCPServer:
    def __init__(self):
        self.browser: Optional[Browser] = None
        self.page: Optional[Page] = None
        self.playwright = None

    async def start_browser(self):
        """Start Playwright browser if not already running."""
        if not self.browser:
            self.playwright = await async_playwright().start()
            self.browser = await self.playwright.chromium.launch(headless=HEADLESS)
            self.page = await self.browser.new_page()
            log(f"✅ Browser started (headless={HEADLESS})")

    async def navigate(self, url: str) -> dict:
        """Navigate to a URL."""
        await self.start_browser()
        await self.page.goto(url, wait_until="domcontentloaded")
        return {"success": True, "url": self.page.url}

    async def get_snapshot(self) -> dict:
        """
        Get a rich snapshot of the current page for planning.
        On error, return a fallback accessibility snapshot plus a 'warning' with error and trace.
        """
        await self.start_browser()

        # Make sure page is really ready.
        try:
            await self.page.wait_for_load_state("domcontentloaded", timeout=10000)
        except Exception:
            pass

        # Minimal probe to detect evaluate issues early.
        try:
            _title_probe = await self.page.evaluate("() => document.title")
        except Exception as e:
            raise RuntimeError(f"Basic evaluate failed: {e}")

        # Rich DOM snapshot JS
        js = """
(() => {
  const isVisible = (el) => {
    if (!el) return false;
    const style = window.getComputedStyle(el);
    if (style.visibility === 'hidden' || style.display === 'none' || parseFloat(style.opacity || '1') === 0) return false;
    const rect = el.getBoundingClientRect();
    if (!rect || rect.width === 0 || rect.height === 0) return false;
    return true;
  };

  const clsSelector = (cls) => cls ? '.' + cls.trim().split(/\\s+/).join('.') : '';
  const esc = (s) => String(s).replace(/["\\\\]/g, m => '\\\\' + m);

  const buildSelectorsForInput = (el) => {
    const ph = el.getAttribute('placeholder');
    const cls = el.getAttribute('class');
    const id  = el.id;
    const sels = [];
    if (id) sels.push(`#${id}`);
    if (cls && ph) sels.push(`input${clsSelector(cls)}[placeholder="${esc(ph)}"]`);
    if (ph) sels.push(`input[placeholder="${esc(ph)}"]`);
    if (ph) sels.push(`input[placeholder*="${esc(ph.split(' ')[0])}" i]`);
    if (cls) sels.push(`input${clsSelector(cls)}`);
    // generic fallbacks:
    sels.push("main input[type='search'], main input[placeholder*='Search' i]");
    sels.push("input[type='search'], input[placeholder*='Search' i]");
    return Array.from(new Set(sels));
  };

  const buildSelectorsForLink = (el) => {
    const href = el.getAttribute('href') || '';
    const cls  = el.getAttribute('class');
    const id   = el.id;
    const sels = [];
    if (id) sels.push(`#${id}`);
    if (cls) sels.push(`a${clsSelector(cls)}`);
    if (href) sels.push(`a[href="${esc(href)}"]`);
    if (href.includes('/d/')) sels.push(`a[href*="/d/"]`);
    if (href.startsWith('/d/')) sels.push(`a[href^="/d/"]`);
    // catalog-ish defaults:
    sels.push("li.listing a[href^='/d/']");
    sels.push(".search-results a[href*='/d/']");
    return Array.from(new Set(sels));
  };

  const buildSelectorsForButton = (el) => {
    const text = (el.innerText || '').trim();
    const cls  = el.getAttribute('class');
    const id   = el.id;
    const sels = [];
    if (id) sels.push(`#${id}`);
    if (cls) sels.push(`button${clsSelector(cls)}`);
    if (text) sels.push(`button:has-text("${esc(text)}")`);
    return Array.from(new Set(sels));
  };

  const takeText = (el) => (el.innerText || el.textContent || '').trim().slice(0, 200);

  const gather = (selector, roleHint) => {
    const out = [];
    document.querySelectorAll(selector).forEach(el => {
      const tag = el.tagName.toLowerCase();
      const role = roleHint || el.getAttribute('role') ||
                   (tag === 'input' ? 'textbox' : tag === 'a' ? 'link' : tag === 'button' ? 'button' : null);
      const name = el.getAttribute('aria-label') || el.getAttribute('title') ||
                   el.getAttribute('placeholder') || takeText(el) || '';

      const attrs = {
        tag,
        type: el.getAttribute('type') || null,
        placeholder: el.getAttribute('placeholder') || null,
        class: el.getAttribute('class') || null,
        id: el.id || null,
        ariaLabel: el.getAttribute('aria-label') || null,
        href: tag === 'a' ? (el.getAttribute('href') || null) : null,
        text: tag !== 'input' ? takeText(el) : null
      };

      let selectors = [];
      if (tag === 'input') selectors = buildSelectorsForInput(el);
      else if (tag === 'a') selectors = buildSelectorsForLink(el);
      else if (tag === 'button') selectors = buildSelectorsForButton(el);

      out.push({
        role,
        name,
        visible: isVisible(el),
        attrs,
        selectors
      });
    });
    return out;
  };

  const inputs  = gather("input[type='search'], input[placeholder], input[type='text']", "textbox");
  const links   = gather("a[href]", "link");
  const buttons = gather("button, [role='button']", "button");
  const elements = [...inputs, ...buttons, ...links];

  // Pick a good search input (visible; search-like; prefer autosuggest/hero classes)
  const searchCandidates = inputs.filter(e =>
    e.visible && e.attrs && (
      (e.attrs.type && e.attrs.type.toLowerCase() === 'search') ||
      (e.attrs.placeholder && /search|find/i.test(e.attrs.placeholder)) ||
      (e.name && /search|find/i.test(e.name))
    )
  );
  const pickBestSearch = (arr) => {
    let best = null; let score = -1;
    for (const e of arr) {
      const cls = (e.attrs.class || '');
      // class heuristic: autosuggest/hero/searchbar preferred
      let s = 0;
      if (/autosuggest|hero|searchbar/i.test(cls)) s += 2;
      if (e.attrs.placeholder) s += 1;
      if (e.attrs.type && e.attrs.type.toLowerCase() === 'search') s += 1;
      if (s > score) { score = s; best = e; }
    }
    return best || arr[0] || null;
  };

  return {
    url: window.location.href,
    title: document.title,
    elements,
    element_count: elements.length,
    recommended: {
      searchInput: pickBestSearch(searchCandidates),
      resultsSelectorCandidates: [
        "li.listing a[href^='/d/']",
        ".search-results a[href*='/d/']",
        "a[href*='/d/']"
      ]
    }
  };
})();
        """

        # Try rich snapshot; if it fails, capture error + trace and fallback.
        try:
            data = await self.page.evaluate(js)
            url = data.get("url") or self.page.url
            title = data.get("title") or await self.page.title()
            context = {
                "url": url,
                "title": title,
                "elements": data.get("elements", []),
                "element_count": data.get("element_count", 0),
                "recommended": data.get("recommended", {}),
            }
            return context
        except Exception as e:
            err = str(e)
            tb = traceback.format_exc()
            log(f"[SNAPSHOT ERROR] {err}\n{tb}")

            # Fallback: accessibility snapshot so caller still gets usable data.
            try:
                ax = await self.page.accessibility.snapshot()
            except Exception as ax_e:
                # If even accessibility fails, return minimal info
                return {
                    "url": self.page.url,
                    "title": await self.page.title(),
                    "elements": [],
                    "element_count": 0,
                    "warning": {
                        "rich_snapshot_error": err,
                        "trace": tb,
                        "accessibility_error": str(ax_e),
                        "note": "Both rich and accessibility snapshots failed."
                    }
                }

            elements = []

            def walk(n):
                if not isinstance(n, dict):
                    return
                role = n.get("role")
                name = n.get("name") or ""
                if role in {"textbox", "button", "link", "combobox", "menuitem", "checkbox", "radio"}:
                    elements.append({"role": role, "name": name})
                for c in (n.get("children") or []):
                    walk(c)

            walk(ax)
            return {
                "url": self.page.url,
                "title": await self.page.title(),
                "elements": elements,
                "element_count": len(elements),
                "recommended": {
                    "resultsSelectorCandidates": [
                        "li.listing a[href^='/d/']",
                        ".search-results a[href*='/d/']",
                        "a[href*='/d/']"
                    ]
                },
                "warning": {
                    "rich_snapshot_error": err,
                    "trace": tb,
                    "note": "Returned fallback accessibility snapshot."
                }
            }

    async def click(self, selector: str) -> dict:
        """Click an element."""
        await self.start_browser()
        await self.page.click(selector)
        return {"success": True}

    async def fill(self, selector: str, text: str) -> dict:
        """
        Fill a text input, but do it like a user:
          - click to focus
          - select-all + backspace
          - type with small delay (to trigger SPA key listeners)
        """
        await self.start_browser()
        el = self.page.locator(selector).first
        await el.scroll_into_view_if_needed()
        await el.click()
        # clear
        try:
            await self.page.keyboard.down("Meta"); await self.page.keyboard.press("KeyA"); await self.page.keyboard.up("Meta")
        except Exception:
            try:
                await self.page.keyboard.down("Control"); await self.page.keyboard.press("KeyA"); await self.page.keyboard.up("Control")
            except Exception:
                pass
        await self.page.keyboard.press("Backspace")
        # type
        await el.type(text, delay=10)
        return {"success": True}

    async def cleanup(self):
        """Clean up browser resources."""
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()


async def main():
    # Create server instance
    server = Server("playwright-mcp-server")
    pw_server = PlaywrightMCPServer()

    # Register tools
    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return [
            Tool(
                name="playwright_navigate",
                description="Navigate to a URL in the browser",
                inputSchema={
                    "type": "object",
                    "properties": {"url": {"type": "string", "description": "The URL to navigate to"}},
                    "required": ["url"],
                },
            ),
            Tool(
                name="playwright_snapshot",
                description="Get a rich snapshot of the current page (elements + attributes + selectors). "
                            "On error, returns a fallback snapshot plus a 'warning' with error and trace.",
                inputSchema={"type": "object", "properties": {}},
            ),
            Tool(
                name="playwright_click",
                description="Click an element on the page",
                inputSchema={
                    "type": "object",
                    "properties": {"selector": {"type": "string", "description": "CSS selector for the element"}},
                    "required": ["selector"],
                },
            ),
            Tool(
                name="playwright_fill",
                description="Fill a text input field (clears and types to trigger SPA listeners)",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "selector": {"type": "string", "description": "CSS selector for the input"},
                        "text": {"type": "string", "description": "Text to fill"},
                    },
                    "required": ["selector", "text"],
                },
            ),
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: Any) -> list[TextContent]:
        try:
            if name == "playwright_navigate":
                result = await pw_server.navigate(arguments["url"])
            elif name == "playwright_snapshot":
                result = await pw_server.get_snapshot()
            elif name == "playwright_click":
                result = await pw_server.click(arguments["selector"])
            elif name == "playwright_fill":
                result = await pw_server.fill(arguments["selector"], arguments["text"])
            else:
                raise ValueError(f"Unknown tool: {name}")

            return [TextContent(type="text", text=json.dumps(result))]
        except Exception as e:
            # Surface server-side errors with a full traceback to the client.
            err = {"error": str(e), "trace": traceback.format_exc()}
            log(f"[TOOL ERROR] {err['error']}\n{err['trace']}")
            return [TextContent(type="text", text=json.dumps(err))]

    log("🚀 Starting Playwright MCP server on stdio...")

    # Run server
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )

    # Cleanup
    await pw_server.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
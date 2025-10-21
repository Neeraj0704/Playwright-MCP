# ai_agent.py
from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional, Tuple

# --- Quiet noisy logs early (optional but nice) ---
os.environ.setdefault("GLOG_minloglevel", "2")
os.environ.setdefault("GRPC_VERBOSITY", "NONE")
try:
    import warnings
    from urllib3.exceptions import NotOpenSSLWarning  # type: ignore
    warnings.filterwarnings("ignore", category=NotOpenSSLWarning)
except Exception:
    pass

# --- .env loading ---
from dotenv import load_dotenv
load_dotenv()  # loads GEMINI_API_KEY/GOOGLE_API_KEY if present

from jsonschema import validate, Draft7Validator
from jsonschema.exceptions import ValidationError
from tenacity import retry, stop_after_attempt, wait_exponential
from playwright.sync_api import Page, TimeoutError as PWTimeout

# ======================================================
# JSON schema for an AI plan (MCP-style structured cmds)
# ======================================================
ACTION_SCHEMA: Dict[str, Any] = {
    "type": "array",
    "items": {
        "type": "object",
        "required": ["action"],
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "goto", "fill", "click", "press", "wait_for",
                    "extract", "assert_text", "select_option",
                    "scroll", "sleep"
                ],
            },
            "url": {"type": "string"},
            "selector": {"type": "string"},
            "role": {"type": "string"},
            "name": {"type": "string"},
            "value": {"type": "string"},
            "assert": {"type": "string"},
            "timeout_ms": {"type": "number"},
        },
    },
}
_validator = Draft7Validator(ACTION_SCHEMA)

# ======================================================
# MCP-ish context builder (swap with real MCP later)
# ======================================================
# ai_agent.py (replace build_page_context with this version)

def build_page_context(page: Page) -> Dict[str, Any]:
    """
    Try MCP first (if configured) to build a rich page context the LLM can use.
    If MCP is not available or fails, fall back to the local accessibility snapshot.
    """
    print("[MCP DEBUG] 🧠 Building MCP-style page context...")
    url = page.url
    print(f"[MCP DEBUG] Current URL: {url}")

    # ---------- MCP-FIRST PATH ----------
    try:
        from mcp_client import build_playwright_mcp  # type: ignore

        # helper: run anyio.run(coro) safely in a separate thread to avoid loop clashes
        def _run_anyio_in_thread(coro_factory):
            from threading import Thread
            from queue import Queue
            q: "Queue[tuple[str, object]]" = Queue()

            def worker():
                try:
                    import anyio  # type: ignore
                    res = anyio.run(coro_factory)
                    q.put(("ok", res))
                except Exception as e:
                    q.put(("err", e))

            t = Thread(target=worker, daemon=True)
            t.start()
            t.join()
            status, payload = q.get()
            if status == "ok":
                return payload
            raise payload  # re-raise original exception

        mcp = build_playwright_mcp()
        if mcp:
            print("[MCP DEBUG] Attempting to use Playwright MCP Server via stdio...")

            async def _run():
                # async section executed inside the thread's fresh event loop
                from mcp_client import PlaywrightMCP  # type: ignore
                async with mcp as client:  # t
                    try:
                        ok = await client.navigate(url)
                        print(f"[MCP DEBUG] MCP navigate ok: {ok}")
                    except Exception as ne:
                        print(f"[MCP DEBUG] MCP navigate failed (will still try context): {ne}")
                    data = await client.get_page_context()
                    # also log available tools once (optional)
                    try:
                        tools = client.list_tool_names()
                        print(f"[MCP DEBUG] Tools available: {tools}")
                    except Exception:
                        pass
                    return data

            data = _run_anyio_in_thread(_run)
            if data and isinstance(data, dict) and data.get("elements"):
                data["__source"] = "mcp"
                print(f"[MCP DEBUG] ✅ Using Playwright MCP Server for page context "
                      f"(elements={len(data.get('elements', []))})")
                return data
            else:
                print("[MCP DEBUG] MCP returned no usable elements → falling back")
        else:
            print("[MCP DEBUG] MCP not configured (set MCP_PLAYWRIGHT_CMD) → falling back")
    except Exception as e:
        print(f"[MCP DEBUG] MCP path failed: {e} → falling back")

    # ---------- LOCAL FALLBACK (unchanged) ----------
    print("[MCP DEBUG] ❌ MCP not available → using local accessibility snapshot")
    try:
        ax = page.accessibility.snapshot() or {}
        print("[MCP DEBUG] Accessibility snapshot captured ✅")
    except Exception as e:
        print(f"[MCP DEBUG] Accessibility snapshot failed ❌ {e}")
        ax = {}

    elements: List[Dict[str, Any]] = []

    def walk(node):
        if not isinstance(node, dict):
            return
        role = node.get("role")
        name = node.get("name")
        if role in {"textbox", "button", "link", "combobox", "menuitem", "checkbox", "radio", "img"}:
            elements.append({"role": role, "name": name})
        for child in (node.get("children") or []):
            walk(child)

    walk(ax)
    print(f"[MCP DEBUG] Extracted {len(elements)} accessible elements")

    testids = []
    try:
        testids = page.eval_on_selector_all(
            "[data-testid]",
            "els => els.map(e => ({testid: e.getAttribute('data-testid')}))"
        ) or []
        print(f"[MCP DEBUG] Found {len(testids)} data-testid elements")
    except Exception as e:
        print(f"[MCP DEBUG] Failed to collect testids: {e}")

    has_search_box = page.locator("input[placeholder*='Search' i], input[type='search']").count() > 0
    has_submit_btn = page.locator("button[type='submit'], button:has(svg)").count() > 0

    print(f"[MCP DEBUG] Search box: {has_search_box}, Submit button: {has_submit_btn}")

    ctx = {
        "__source": "local",
        "url": url,
        "elements": elements,
        "testids": testids,
        "hints": {
            "has_search_box": bool(has_search_box),
            "has_submit_btn": bool(has_submit_btn),
        },
    }

    print("[MCP DEBUG] ✅ Context built successfully (local)")
    return ctx



# ======================================================
# LLM planner (Gemini first; fallback to heuristic)
#   -> now returns (plan, meta) so you can see what was used
# ======================================================

def _try_gemini_plan(goal: str, context: Dict[str, Any]) -> Optional[Tuple[List[Dict[str, Any]], Dict[str, Any]]]:
    """
    Try multiple known-good Gemini model IDs (based on what your key lists).
    Returns (plan, meta) or None on any error so caller can fallback.
    """
    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        return None

    try:
        import google.generativeai as genai  # type: ignore
        genai.configure(api_key=api_key)
    except Exception:
        return None

    MODEL_CANDIDATES = [
        "models/gemini-2.5-flash",
        "models/gemini-2.5-flash-lite",
        "models/gemini-2.5-pro",
    ]

    # ⭐ IMPROVED PROMPT - This is the key change!
    system = (
    "You are a WEB AUTOMATION PLANNER for https://data.lacity.org.\n\n"
    "Goal: Generate a JSON plan (array) to search for datasets and extract results.\n\n"
    "MUST follow this pattern:\n"
    "1️⃣ wait_for → search input\n"
    "2️⃣ fill → search box with user query\n"
    "3️⃣ press → Enter\n"
    "4️⃣ wait_for → dataset result links\n"
    "5️⃣ extract → result titles & URLs\n\n"
    "Selectors to ALWAYS use:\n"
    "   - Search input: input.react-autosuggest__input[placeholder='Search for Data']\n"
    "   - Result links: a[href*='/d/']\n\n"
    "Timeouts:\n"
    "   - wait_for input → 7000ms\n"
    "   - wait_for results → 15000ms\n\n"
    "Rules:\n"
    "• Use only these 5 steps (no goto).\n"
    "• Fill using the keyword form of the goal (not the full sentence).\n"
    "• Output only valid JSON, no markdown or extra text.\n\n"
    "Example:\n"
    "[\n"
    "  {\"action\": \"wait_for\", \"selector\": \"input.react-autosuggest__input[placeholder='Search for Data']\", \"timeout_ms\": 7000},\n"
    "  {\"action\": \"fill\", \"selector\": \"input.react-autosuggest__input[placeholder='Search for Data']\", \"value\": \"<keywords>\"},\n"
    "  {\"action\": \"press\", \"selector\": \"input.react-autosuggest__input[placeholder='Search for Data']\", \"value\": \"Enter\"},\n"
    "  {\"action\": \"wait_for\", \"selector\": \"a[href*='/d/']\", \"timeout_ms\": 15000},\n"
    "  {\"action\": \"extract\", \"selector\": \"a[href*='/d/']\"}\n"
    "]"
)
    
    user = {"goal": goal, "context": context}

    for mid in MODEL_CANDIDATES:
        try:
            model = genai.GenerativeModel(mid)
            resp = model.generate_content([system, json.dumps(user)])
            text = (getattr(resp, "text", "") or "").strip()
            if not text:
                continue

            # Extract pure JSON array
            if not text.startswith("["):
                s, e = text.find("["), text.rfind("]")
                if s == -1 or e == -1:
                    continue
                text = text[s:e + 1]

            plan = json.loads(text)
            _validator.validate(plan)  # raises if invalid
            
            # ⭐ VALIDATION: Check if plan includes extraction
            has_extract = any(step.get("action") == "extract" for step in plan)
            if not has_extract:
                print(f"[AI WARN] Plan missing extract step, trying next model...")
                continue
            
            meta = {"source": "gemini", "model": mid}
            return plan, meta
        except Exception as e:
            print(f"[AI DEBUG] Model {mid} failed: {e}")
            continue

    return None


# ⭐ IMPROVED FALLBACK - Always includes extraction
def _fallback_plan(goal: str, context: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [
        {"action": "wait_for", "selector": "input[placeholder*='Search' i]", "timeout_ms": 7000},
        {"action": "fill", "selector": "input[placeholder*='Search' i]", "value": goal},
        {"action": "press", "selector": "input[placeholder*='Search' i]", "value": "Enter"},
        {"action": "wait_for", "selector": "a[href*='/d/']", "timeout_ms": 15000},
        {"action": "extract", "selector": "a[href*='/d/']"},  # ⭐ ALWAYS EXTRACT!
    ]

def plan_actions_via_llm_mcp(goal: str, context: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Return (plan, meta). meta includes:
      - source: "gemini" or "fallback"
      - model: model id if source == "gemini"
    """
    tried = _try_gemini_plan(goal, context)
    if tried is not None:
        plan, meta = tried
    else:
        plan, meta = _fallback_plan(goal, context), {"source": "fallback", "model": None}

    try:
        validate(instance=plan, schema=ACTION_SCHEMA)
    except ValidationError:
        plan, meta = _fallback_plan(goal, context), {"source": "fallback", "model": None}
    return plan, meta

# ======================================================
# Action execution helpers
# ======================================================

def _loc(page: Page, step: Dict[str, Any]):
    role = step.get("role")
    name = step.get("name")
    if role and name:
        return page.get_by_role(role=role, name=name)
    sel = step.get("selector")
    if sel:
        return page.locator(sel).first
    raise RuntimeError("Step needs either (role+name) or selector")

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=0.5, max=3))
def _do_wait_for(page: Page, step: Dict[str, Any]):
    loc = _loc(page, step)
    timeout = step.get("timeout_ms", 7000)
    loc.wait_for(state="visible", timeout=timeout)

def _do_goto(page: Page, step: Dict[str, Any]):
    url = step.get("url")
    if not url:
        raise RuntimeError("goto requires 'url'")
    page.goto(url, wait_until="domcontentloaded", timeout=30000)

@retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=0.5, max=2))
def _do_fill(page: Page, step: Dict[str, Any]):
    loc = _loc(page, step)
    val = step.get("value", "")
    loc.fill(val)

@retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=0.5, max=2))
def _do_click(page: Page, step: Dict[str, Any]):
    loc = _loc(page, step)
    loc.click()

def _do_press(page: Page, step: Dict[str, Any]):
    loc = _loc(page, step)
    key = step.get("value", "Enter")
    loc.press(key)

def _do_scroll(page: Page, step: Dict[str, Any]):
    page.mouse.wheel(0, 1200)
    time.sleep(0.3)

def _do_sleep(page: Page, step: Dict[str, Any]):
    ms = int(step.get("timeout_ms", 500))
    time.sleep(ms / 1000.0)

def _do_assert_text(page: Page, step: Dict[str, Any]):
    loc = _loc(page, step)
    expected = step.get("assert", "")
    actual = loc.inner_text().strip()
    if expected not in actual:
        raise AssertionError(
            f"assert_text failed. Expected to contain '{expected}', got '{actual[:120]}'"
        )

def _do_extract(page: Page, step: Dict[str, Any]) -> Dict[str, Any]:
    loc = _loc(page, step)
    text = loc.inner_text().strip().replace("\n", " ")
    href = None
    try:
        href = loc.get_attribute("href")
        if href and not href.startswith("http"):
            href = f"https://data.lacity.org{href}"
    except Exception:
        pass
    return {"text": text, "href": href}

ACTION_MAP = {
    "goto": _do_goto,
    "fill": _do_fill,
    "click": _do_click,
    "press": _do_press,
    "wait_for": _do_wait_for,
    "scroll": _do_scroll,
    "sleep": _do_sleep,
    "assert_text": _do_assert_text,
    # "extract" handled specially (returns data) below
}

def execute_steps(page: Page, steps: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Execute a plan. Returns a dict with optional 'extracted' field when the plan
    includes an 'extract' action.
    """
    result: Dict[str, Any] = {"extracted": None}

    for step in steps:
        action = step.get("action")
        if not action:
            continue

        if action == "extract":
            result["extracted"] = _do_extract(page, step)
            continue

        fn = ACTION_MAP.get(action)
        if not fn:
            continue

        try:
            fn(page, step)
        except PWTimeout as te:
            raise te
        except Exception:
            # Soft-fail non-timeout hiccups; continue
            pass

        if action in {"goto", "click"}:
            try:
                page.wait_for_load_state("domcontentloaded", timeout=15000)
            except PWTimeout:
                pass

    return result

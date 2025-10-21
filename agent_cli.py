import argparse
import json
import sys
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import quote_plus

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from ai_agent import build_page_context, plan_actions_via_llm_mcp, execute_steps

START_URL = "https://data.lacity.org/"

def _save_plan(plan, meta):
    """Save the plan + meta for inspection when --debug is on."""
    out = Path("debug")
    out.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    f = out / f"plan_{ts}.json"
    with f.open("w", encoding="utf-8") as fp:
        json.dump({"meta": meta, "plan": plan}, fp, indent=2)
    return f


def extract_first_result(page):
    """Fallback extractor if AI plan doesn't include extraction."""
    title_sel = "a[href*='/d/']"
    page.locator(title_sel).first.wait_for(state="visible", timeout=15000)
    title_node = page.locator(title_sel).first
    title = title_node.inner_text().strip().replace("\n", " ")
    href = title_node.get_attribute("href")
    if href and not href.startswith("http"):
        href = f"https://data.lacity.org{href}"
    return title, href or ""


def _extract_search_text(text: str) -> str:
    """
    Turn a natural sentence into a short keyword query.
    Examples:
      "I want to know about the crimes in LA" -> "crimes LA"
      "Find datasets on Department of General Services" -> "Department of General Services"
    """
    if not text:
        return ""

    # Normalize spaces
    s = re.sub(r"\s+", " ", text).strip()

    # Keep quoted phrases intact first (e.g., "general services")
    phrases = re.findall(r'"([^"]+)"|\'([^\']+)\'', s)
    phrase_tokens = [p[0] or p[1] for p in phrases]

    # Remove quotes from the main string
    s = re.sub(r'["\']', "", s)

    # Remove leading filler phrases
    s = re.sub(
        r"^(i\s*(want|would\s*like|need)\s*(to\s*(know|see|find|search))?|tell\s*me|show\s*me|can\s*you\s*(find|show))\s*(about|for)?\s*",
        "",
        s,
        flags=re.I,
    )

    # Common stopwords (short list to keep it simple)
    STOP = {
        "the","a","an","and","or","of","on","in","for","to","from","with","about",
        "at","by","into","is","are","was","were","be","been","being","that","this",
        "these","those","it","its","as","i","we","you","they","he","she","them","me",
        "my","our","your","their","want","would","like","know","find","search","see",
        "please","dataset","datasets","data"
    }

    # Tokenize words, keep alphanumerics and hyphens
    tokens = re.findall(r"[A-Za-z0-9\-]+", s)

    # Keep capitalized multi-word entities (simple heuristic)
    # Also keep short important words like LA/LAPD
    cleaned = []
    for t in tokens:
        if t.lower() in STOP:
            continue
        if len(t) <= 2 and t.upper() not in {"LA", "PD", "DPW"}:
            continue
        cleaned.append(t)

    # Reattach any quoted phrases we captured
    if phrase_tokens:
        cleaned.extend(phrase_tokens)

    # Deduplicate, preserve order
    seen = set()
    deduped = []
    for t in cleaned:
        if t.lower() not in seen:
            seen.add(t.lower())
            deduped.append(t)

    # Prefer at most ~3–5 tokens to keep search tight
    if len(deduped) > 5:
        deduped = deduped[:5]

    # Special-case: normalize "la" to "LA"
    deduped = ["LA" if t.lower() == "la" else t for t in deduped]

    # Join back into a query string
    return " ".join(deduped).strip()



def run_goal(goal: str, debug: bool = False, use_mcp_reread: bool = True):
    """
    Main execution flow.

    RETURNS:
        List[{"title": str, "url": str}]  (up to 10 items; [] on error)
    """
    results = []

    with sync_playwright() as p:
        # Headless for UI/server usage
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        try:
            # 1) Open portal
            print(f"\n{'='*60}")
            print(f"[STEP 1] 🌐 Opening LA Data Portal")
            print(f"{'='*60}\n")
            page.goto(START_URL, wait_until="domcontentloaded", timeout=30000)

            # 2) Build initial MCP context (homepage)
            print("[STEP 2] 📊 Building initial page context via MCP...")
            context = build_page_context(page)
            print(f"   ✅ Context source: {context.get('__source')}")
            print(f"   ✅ Elements found: {len(context.get('elements', []))}")

            if debug and len(context.get("elements", [])) > 0:
                print(f"   📋 Sample elements:")
                for elem in context["elements"][:5]:
                    print(f"      - {elem.get('role')}: {elem.get('name')}")

            # 3) Ask AI for complete plan (keywordized)
            print(f"\n[STEP 3] 🧠 AI Planning...")
            kw_goal = _extract_search_text(goal)
            print(f"   🔎 Using keywordized query: {kw_goal!r}")
            steps, meta = plan_actions_via_llm_mcp(kw_goal, context)
            print(f"   ✅ Plan created by: {meta.get('source')} ({meta.get('model', 'N/A')})")
            print(f"   ✅ Steps planned: {len(steps)}")

            if debug:
                print("\n[DEBUG] Full plan:")
                print(json.dumps(steps, indent=2))
                saved = _save_plan(steps, meta)
                print(f"[DEBUG] Saved to: {saved}")

            # 4) Execute plan step by step (capture any 'extract' result, but don't return early)
            print(f"\n[STEP 4] ▶️  Executing plan...")
            first_hit = None

            for i, step in enumerate(steps, 1):
                action = step.get("action")
                print(f"   [{i}/{len(steps)}] {action}...", end=" ")
                try:
                    result = execute_steps(page, [step])  # Execute just this one step
                    print("✅")

                    # If the plan's 'extract' produced something, keep it
                    if result.get("extracted"):
                        eh = result["extracted"]
                        if eh.get("text") and eh.get("href"):
                            first_hit = {
                                "title": eh["text"],
                                "url": eh["href"] if eh["href"].startswith("http")
                                       else f"https://data.lacity.org{eh['href']}",
                            }

                    # Optional: re-read after potential navigation actions
                    if use_mcp_reread and action in {"press", "click", "goto"}:
                        try:
                            page.wait_for_load_state("domcontentloaded", timeout=5000)
                        except PWTimeout:
                            pass

                except PWTimeout:
                    print("⏱️  TIMEOUT")
                except Exception as e:
                    print(f"⚠️  Error: {e}")

            # 5) Ensure results page; then collect up to 10 results directly (no helper)
            print("\n[STEP 5] 🔍 Checking for results...")
            if "browse" not in page.url:
                print("   ⚠️  Not on results page, navigating...")
                page.goto(
                    f"https://data.lacity.org/browse?limitTo=datasets&q={quote_plus(kw_goal)}",
                    wait_until="domcontentloaded",
                    timeout=30000,
                )

            # Try to read result anchors
            sel = "a[href*='/d/']"
            try:
                page.locator(sel).first.wait_for(state="visible", timeout=15000)
            except Exception:
                pass

            try:
                anchors = page.locator(sel).all()
            except Exception:
                anchors = []

            # Build up to 10 result items
            for a in anchors[:10]:
                try:
                    title = (a.inner_text() or "").strip().replace("\n", " ")
                    href = a.get_attribute("href")
                    if not title or not href:
                        continue
                    url = href if href.startswith("http") else f"https://data.lacity.org{href}"
                    results.append({"title": title, "url": url})
                except Exception:
                    continue

            # If AI extract found something unique, prepend it
            if first_hit and all(first_hit["url"] != r["url"] for r in results):
                results = [first_hit] + results
                results = results[:10]

            # Final fallback: if still nothing, try your extract_first_result()
            if not results:
                try:
                    title, url = extract_first_result(page)
                    if title and url:
                        results = [{"title": title, "url": url}]
                except Exception:
                    results = []

        except PWTimeout as e:
            print(f"\n❌ TIMEOUT: {e}")
            results = []
        except Exception as e:
            print(f"\n❌ ERROR: {e}")
            import traceback
            traceback.print_exc()
            results = []
        finally:
            print("[CLEANUP] 🧹 Closing browser")
            browser.close()

    return results


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="AI-driven Playwright agent CLI")
    ap.add_argument("--goal", "-g", type=str, required=False, 
                    help="User goal in plain English")
    ap.add_argument("--debug", action="store_true", 
                    help="Print and save the AI/fallback plan JSON")
    ap.add_argument("--no-mcp-reread", action="store_true",
                    help="Disable MCP re-reading after navigation (not recommended)")
    args = ap.parse_args()

    goal = args.goal
    if not goal:
        goal = input("Enter your goal (e.g., 'Department of General Services'):\n> ").strip()
    if not goal:
        print("No goal provided.")
        sys.exit(1)

    run_goal(goal, debug=args.debug, use_mcp_reread=not args.no_mcp_reread)
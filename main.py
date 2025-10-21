from pathlib import Path
from datetime import datetime
from urllib.parse import quote_plus
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from ai_agent import build_page_context, plan_actions_via_llm_mcp, execute_steps

# ----------------------------
# CONFIG
# ----------------------------
START_URL = "https://data.lacity.org/"
QUERY = "Department of General Services"

# ----------------------------
# UTILITY FUNCTIONS
# ----------------------------

def save_debug(page, label: str):
    """Save screenshot + HTML dump for debugging."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = Path("debug")
    out.mkdir(exist_ok=True)
    img = out / f"{ts}_{label}.png"
    html = out / f"{ts}_{label}.html"
    page.screenshot(path=str(img), full_page=True)
    html.write_text(page.content(), encoding="utf-8")
    print(f"[debug] Saved {img} and {html}")

def first_visible(page, selectors: list[str], timeout_each=4000):
    """Return the first locator that becomes visible among selector candidates."""
    for sel in selectors:
        loc = page.locator(sel).first
        try:
            loc.wait_for(state="visible", timeout=timeout_each)
            return loc
        except PWTimeout:
            continue
    return None

# ----------------------------
# MAIN AUTOMATION SCRIPT
# ----------------------------

def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        page = browser.new_page()
        try:
            # 1️⃣ Go to URL  (✔ brief: Go to URL)
            page.goto(START_URL, wait_until="domcontentloaded", timeout=30000)
            print(f"Opened website: {page.title()}")

            # 2️⃣ Type in the search box  (✔ brief: Type)
            search_input = page.locator(
                "input[placeholder='Search'], input[type='search'], input[aria-label='Search']"
            ).first
            search_input.wait_for(state="visible", timeout=7000)
            search_input.fill(QUERY)
            print(f"Typed query: {QUERY}")

            # 3️⃣ Click search button (or press Enter fallback)  (✔ brief: Click)
            search_button = page.locator(
                "button[type='submit'], button[aria-label='Search'], form button:has(svg)"
            ).first
            try:
                search_button.wait_for(state="visible", timeout=2000)
                search_button.click()
                print("Clicked search button.")
            except PWTimeout:
                search_input.press("Enter")
                print("Search button not visible → pressed Enter.")

            # 4️⃣ Wait for results page to load (✔ reliability)
            page.wait_for_load_state("domcontentloaded", timeout=20000)
            print(f"Results page loaded (basic). URL is: {page.url}")

            # 5️⃣ Extract dataset title (✔ data extraction)
            title_candidates = [
                "div.browse2-result-card__title a",
                "a[href*='/datasets/']",
                "a[href*='/d/']",
                "a[href*='/resource/']",
                "[data-testid='browse2-result-name']",
                ".browse2-result-name",
                "article [data-testid='result-name']",
                "a.socrata-card__title",
            ]
            title_loc = first_visible(page, title_candidates, timeout_each=4000)

            # Fallback to explicit Browse URL if search didn’t redirect properly
            if not title_loc:
                browse_url = f"https://data.lacity.org/browse?limitTo=datasets&q={quote_plus(QUERY)}"
                print(f"No results detected yet. Navigating to Browse: {browse_url}")
                page.goto(browse_url, wait_until="domcontentloaded", timeout=30000)
                title_loc = first_visible(page, title_candidates, timeout_each=5000)

            if not title_loc:
                save_debug(page, "no_result_title")
                raise RuntimeError("Could not locate a dataset title in results.")

            title_text = title_loc.inner_text().strip().replace("\n", " ")

            # 6️⃣ Extract dataset link and description (✔ extended output)
            link_loc = page.locator(
                "div.browse2-result-card__title a, "
                "a[href*='/datasets/'], "
                "a[href*='/d/'], "
                "a[href*='/resource/']"
            ).first

            dataset_url = ""
            try:
                href = link_loc.get_attribute("href")
                if href:
                    dataset_url = href if href.startswith("http") else f"https://data.lacity.org{href}"
            except Exception:
                pass

            desc_loc = page.locator(
                "div.browse2-result-card__description, "
                "[data-testid='browse2-result-description'], "
                ".browse2-result-description"
            ).first
            description = ""
            try:
                description = desc_loc.inner_text().strip().replace("\n", " ")
            except Exception:
                pass

            # 7️⃣ Final clean output  (✔ brief: final clear result)
            print(f"Success! Found dataset: {title_text}")
            if dataset_url:
                print(f"URL: {dataset_url}")
            if description:
                print(f"Description: {description}")

        except PWTimeout as e:
            save_debug(page, "timeout")
            print(f"[Timeout] {e}")
        except Exception as e:
            save_debug(page, "error")
            print(f"[Error] {e}")
        finally:
            browser.close()

def open_portal(page, url):
    """Go to the main portal and wait for it to load."""
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    print(f"Opened website: {page.title()}")

def extract_first_result(page):
    """Extract the first visible dataset title, URL, and description."""
    title_candidates = [
        "div.browse2-result-card__title a",
        "a[href*='/datasets/']",
        "a[href*='/d/']",
        "a[href*='/resource/']",
        "[data-testid='browse2-result-name']",
        ".browse2-result-name",
        "article [data-testid='result-name']",
        "a.socrata-card__title",
    ]

    for sel in title_candidates:
        loc = page.locator(sel).first
        try:
            loc.wait_for(state="visible", timeout=4000)
            title = loc.inner_text().strip().replace("\n", " ")
            href = loc.get_attribute("href")
            url = href if href.startswith("http") else f"https://data.lacity.org{href}"
            
            # Try to get a short description
            desc_loc = page.locator(
                "div.browse2-result-card__description, "
                "[data-testid='browse2-result-description'], "
                ".browse2-result-description"
            ).first
            description = ""
            try:
                description = desc_loc.inner_text().strip().replace("\n", " ")
            except Exception:
                pass

            return title, url, description
        except PWTimeout:
            continue

    raise RuntimeError("Could not locate a dataset title in results.")

def run_ai(goal: str):
    """AI-driven variant: goal -> (plan) -> execute -> extract -> return dict."""
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        page = browser.new_page()
        try:
            # 1) Open the portal
            open_portal(page, START_URL)

            # 2) Build a small context (replace with MCP context once wired)
            context = build_page_context(page)

            # 3) Ask AI (currently dummy or LLM+MCP) for a plan
            steps = plan_actions_via_llm_mcp(goal, context)

            # 4) Execute planned steps
            execute_steps(page, steps)

            # 5) Extract first result using your existing logic
            title, url, desc = extract_first_result(page)

            return {
                "status": "success",
                "mode": "ai",
                "goal": goal,
                "title": title,
                "url": url,
                "description": desc,
            }
        except PWTimeout as e:
            save_debug(page, "timeout_ai")
            return {"status": "timeout", "mode": "ai", "error": str(e)}
        except Exception as e:
            save_debug(page, "error_ai")
            return {"status": "error", "mode": "ai", "error": str(e)}
        finally:
            browser.close()

# ----------------------------
# ENTRY POINT
# ----------------------------
if __name__ == "__main__":
    main()

# LA Data Bot — Playwright + MCP AI Agent + Flask API

This project automates searching the Los Angeles Open Data Portal (https://data.lacity.org) and extracting dataset results.

It includes:
- **Required Core:** A deterministic Playwright “robot” that opens the site, searches, and prints a clear result.
- **Optional Challenge 1 (Bonus):** An AI-driven planner (Gemini) that builds a step plan based on live page context provided via **MCP** (Model Context Protocol) using a Playwright MCP server.
- **Optional Challenge 2 (API + Frontend):** A minimal Flask API and frontend that exposes a `/search` REST endpoint and a simple HTML interface for user input and dataset display.

---

## 🧩 Project Structure

```plaintext
.
├─ main.py                 # Deterministic Playwright automation
├─ agent_cli.py            # AI-driven CLI for natural-language goals
├─ ai_agent.py             # Planning + execution helpers (LLM + MCP integration)
├─ mcp_client.py           # Minimal MCP client wrapper
├─ mcp_server.py           # Playwright MCP server (rich page snapshot + tools)
├─ app.py                  # Flask API + UI layer (integrates with agent_cli.py)
├─ requirements.txt
├─ .gitignore
└─ debug/                  # Debug artifacts (screenshots, HTML dumps)
---
```

## Prerequisites

- Python 3.10+ recommended  
- Chrome/Chromium is managed by Playwright
- A Google Gemini API key if you want to run the **AI planner** path

---

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# Install Playwright browsers (first time only)
python -m playwright install

Create a .env file if you’ll use the AI planner (Gemini):
# .env
GEMINI_API_KEY=your_gemini_key_here
# or
GOOGLE_API_KEY=your_gemini_key_here


MCP server env (used by AI planner):
# Start the MCP server via python mcp_server.py
MCP_PLAYWRIGHT_CMD=python3
MCP_PLAYWRIGHT_ARGS=mcp_server.py

# See MCP browser UI if desired:
MCP_HEADLESS=0   # default is headless (unset or 1)


How to Run

A) Required Core (deterministic)

This script does a standard navigate → type → click/press → extract flow and prints the result.
python main.py

What it does:
	1.	Opens https://data.lacity.org
	2.	Types a fixed query (e.g., “Department of General Services”) into the search box
	3.	Clicks the search button (or presses Enter)
	4.	Waits for results and prints the first dataset’s title and link
	5.	On issues, it saves a screenshot and HTML dump to debug/

B) Optional Challenge 1 (AI + MCP)

This path uses the MCP server to give the LLM (Gemini) live page context so it can plan the steps (wait_for, fill, press, extract), and then executes them.

1) Start the MCP server via env (the agent will launch it automatically):

No manual step needed if you set the env vars in .env or shell:

MCP_PLAYWRIGHT_CMD=python3
MCP_PLAYWRIGHT_ARGS=mcp_server.py

# example natural goal
python agent_cli.py --goal "I want to know about the crimes in LA" --debug

	•	The agent keywordizes your goal (e.g., “crimes LA”)
	•	It asks the LLM to produce the 5-step plan using the right selectors:
	•	input.react-autosuggest__input[placeholder='Search for Data']
	•	a[href*='/d/']
	•	It executes the plan step-by-step and prints the first hit.
	•	It also navigates to /browse?... if needed and prints up to 10 dataset results.

C) Optional Challenge 2 (Flask Web API + Frontend)

This path provides a minimal Flask application that exposes a `/search` REST endpoint and a simple HTML interface for user interaction.

- Launch the app with:
  ```
  python app.py
  ```
- The `/search` endpoint accepts a query parameter and returns dataset results in JSON.
- The included HTML frontend allows users to input their search query, submit it, and view dataset results dynamically.
- A loading indicator is shown while the search is in progress.
- The Flask app integrates with the same `run_goal` logic from `agent_cli.py` to perform the search and extraction.

⸻


Configuration Notes
	•	Keywordization: agent_cli.py converts natural sentences into a compact query (e.g., “crimes LA”), and uses that both for the LLM plan and for the /browse fallback.
	•	MCP Server: mcp_server.py exposes tools:
	•	playwright_navigate
	•	playwright_snapshot (rich snapshot: roles, attributes, selector candidates)
	•	playwright_click
	•	playwright_fill (clears and types with small delays to trigger SPA listeners)
	•	Robustness: Timeouts and fallbacks (e.g., waiting for DOMContentLoaded; browsing directly to /browse?q=... if search results aren’t visible; debug dumps on failure).
	•	Debug Artifacts: On errors/timeouts, screenshots and HTML dumps are saved in debug/.

⸻

Troubleshooting
	•	Snapshot error from MCP:
The server falls back to an accessibility snapshot and returns a warning payload—your agent still runs. Make sure the MCP_PLAYWRIGHT_CMD/ARGS are set, and try MCP_HEADLESS=0 locally to see the MCP browser.
	•	Fill not working:
The MCP fill tool clicks, selects all, backspaces, and types with a delay to trigger SPA listeners.
	•	Selectors changed on the site:
The LLM is prompted to use the exact stable selectors you’ve configured. If the site changes drastically, adjust those in ai_agent.py prompt text.

⸻


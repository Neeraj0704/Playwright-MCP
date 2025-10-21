# app.py
from flask import Flask, request, jsonify, render_template_string
import os

# Make sure the MCP server can be launched by the agent (Optional Challenge 1)
os.environ.setdefault("MCP_PLAYWRIGHT_CMD", "python3")
os.environ.setdefault("MCP_PLAYWRIGHT_ARGS", "mcp_server.py")

# Import your existing runner (must return a list[{"title","url"}])
from agent_cli import run_goal as run_goal_agent

app = Flask(__name__)

# Simple test UI at "/"
@app.get("/")
def index():
    html = """
    <!doctype html>
    <meta charset="utf-8">
    <title>LA Data Bot</title>
    <style>
      body{font-family:system-ui,Segoe UI,Arial;margin:2rem;max-width:900px}
      form{display:flex;gap:.5rem}
      input[type=text]{flex:1;padding:.6rem;border:1px solid #ccc;border-radius:.5rem}
      button{padding:.6rem 1rem;border:0;border-radius:.5rem;background:#111;color:#fff;cursor:pointer}
      .item{padding:.5rem 0;border-bottom:1px solid #eee}
      .item a{font-weight:600;text-decoration:none}
      #loading{display:none;margin-top:1rem;font-style:italic;color:#555}
    </style>
    <h1>LA Data Bot — Playwright + MCP</h1>
    <p>Enter a goal (e.g., <em>crimes in LA</em>, <em>Department of General Services</em>)</p>
    <form onsubmit="event.preventDefault(); runSearch();">
      <input id="goal" type="text" placeholder="crimes in LA">
      <button>Search</button>
    </form>
    <div id="loading">⏳ Loading results, please wait...</div>
    <div id="results"></div>
    <script>
      async function runSearch() {
        const goal = document.getElementById('goal').value.trim();
        const wrap = document.getElementById('results');
        const loading = document.getElementById('loading');
        wrap.innerHTML = '';
        loading.style.display = 'block';
        try {
          const res = await fetch('/search', {
            method: 'POST',
            headers: {'Content-Type':'application/json'},
            body: JSON.stringify({goal})
          });
          const data = await res.json();
          loading.style.display = 'none';
          if (!Array.isArray(data)) {
            wrap.innerHTML = '<p><b>Error:</b> ' + (data.error || 'Unknown') + '</p>';
            return;
          }
          wrap.innerHTML = data.map(
            (d, i) => `<div class="item"><div>${i+1}. <a href="${d.url}" target="_blank" rel="noopener">${d.title}</a></div></div>`
          ).join('') || '<p>No results.</p>';
        } catch (err) {
          loading.style.display = 'none';
          wrap.innerHTML = '<p><b>Error:</b> ' + err.message + '</p>';
        }
      }
    </script>
    """
    return render_template_string(html)

# JSON REST endpoint the curl command was trying to hit
@app.post("/search")
def search():
    payload = request.get_json(silent=True) or {}
    goal = (payload.get("goal") or request.args.get("goal") or "").strip()
    if not goal:
        return jsonify({"error": "Missing 'goal'"}), 400

    try:
        # run the existing agent (headless in your agent_cli; good for servers)
        results = run_goal_agent(goal, debug=False, use_mcp_reread=True)
        # run_goal returns either list or {"results": list}; normalize:
        if isinstance(results, dict) and "results" in results:
            results = results["results"]
        if not isinstance(results, list):
            return jsonify({"error": "Unexpected agent response"}), 500
        return jsonify(results)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    # Flask dev server
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))

import os
import json
import time
import queue
import threading
import webbrowser
from flask import Flask, render_template_string, request, jsonify, Response
from playwright.sync_api import sync_playwright
import agent_core

app = Flask(__name__)

# Cache current session items
current_session = {
    "items": [],
    "logs": queue.Queue(),
    "results": [],
    "running": False,
    "wait_event": threading.Event(),
    "user_response": None
}

def log_to_web(msg, status="info"):
    current_session["logs"].put({"message": msg, "status": status})

# Dynamic overrides for agent_core print statements
agent_core.info = lambda msg: log_to_web(msg, "info")
agent_core.ok = lambda msg: log_to_web(msg, "success")
agent_core.warn = lambda msg: log_to_web(msg, "warning")
agent_core.err = lambda msg: log_to_web(msg, "error")
agent_core.step = lambda n, total, name: log_to_web(f"[{n}/{total}] {name}", "step")

def web_get_user_input(options, used_q, it):
    serialized_options = []
    for idx, o in enumerate(options):
        # Format labels with price and size if available
        opt_lbl = o["name"]
        if o.get("price") is not None:
            opt_lbl += f" - ${o['price']:.2f}"
        if o.get("size_str"):
            opt_lbl += f" ({o['size_str']})"
        
        serialized_options.append({
            "index": idx + 1,
            "name": o["name"],
            "opt_label": opt_lbl,
            "price": o["price"],
            "is_gv": o.get("is_gv", False),
            "bulk": o.get("bulk", False)
        })
    
    current_session["logs"].put({
        "message": f"Attention required for '{used_q}'! Please select options on the screen.",
        "status": "input_required",
        "options": serialized_options,
        "query": used_q,
        "item": it
    })
    
    current_session["wait_event"].clear()
    current_session["wait_event"].wait()
    return current_session["user_response"]

agent_core.get_user_input = web_get_user_input

@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route("/api/input", methods=["POST"])
def receive_user_input():
    data = request.json
    resp = data.get("response", "").strip()
    current_session["user_response"] = resp
    current_session["wait_event"].set()
    return jsonify({"status": "success"})

@app.route("/api/pantry", methods=["GET", "POST"])
def manage_pantry():
    pantry_file = "pantry.txt"
    if request.method == "POST":
        content = request.json.get("content", "")
        try:
            with open(pantry_file, "w") as f:
                f.write(content)
            return jsonify({"status": "success"})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500
            
    # GET method
    content = ""
    if os.path.exists(pantry_file):
        try:
            with open(pantry_file) as f:
                content = f.read()
        except Exception:
            pass
    return jsonify({"content": content})

@app.route("/api/parse", methods=["POST"])
def parse_list():
    data = request.json
    mode = data.get("mode", "recipe") # 'recipe' or 'paste'
    raw_input = data.get("input", "").strip()
    
    if not raw_input:
        return jsonify({"error": "Input cannot be empty"}), 400
        
    try:
        if mode == "paste":
            # Split raw input lines
            lines = [l.strip() for l in raw_input.split("\n") if l.strip()]
            items = [agent_core.parse_pasted_line(l) for l in lines]
        else:
            items = agent_core.get_grocery_list(raw_input)
            
        items = agent_core.filter_pantry(items)
        current_session["items"] = items
        return jsonify({"items": items})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/clear-cache", methods=["POST"])
def clear_cache():
    if os.path.exists(agent_core.CACHE_FILE):
        try:
            os.remove(agent_core.CACHE_FILE)
            return jsonify({"status": "success", "message": "Cache file cleared."})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500
    return jsonify({"status": "success", "message": "Cache is already empty."})

@app.route("/api/run", methods=["POST"])
def run_playwright():
    if current_session["running"]:
        return jsonify({"error": "Another job is already running."}), 400
        
    # Get possibly modified items from front-end
    items = request.json.get("items", [])
    current_session["items"] = items
    current_session["running"] = True
    current_session["results"] = []
    
    # Clear logs queue
    while not current_session["logs"].empty():
        try:
            current_session["logs"].get_nowait()
        except queue.Empty:
            break
            
    # Start Playwright thread
    thread = threading.Thread(target=playwright_worker, args=(items,))
    thread.daemon = True
    thread.start()
    
    return jsonify({"status": "started"})

def playwright_worker(items):
    log_to_web("Starting Playwright automation process...", "info")
    results = []
    
    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(agent_core.CDP_URL)
            page = browser.contexts[0].pages[0] if browser.contexts[0].pages else browser.contexts[0].new_page()
            log_to_web(f"Connected to Chrome remote debugging on {agent_core.CDP_URL}", "success")
        except Exception as e:
            log_to_web(f"Could not connect to Chrome debugging port at {agent_core.CDP_URL} ({type(e).__name__})", "warning")
            log_to_web("Launching a local Chromium instance with persistent session context...", "info")
            user_data_dir = os.path.join(os.getcwd(), "playwright_user_data")
            try:
                browser_context = p.chromium.launch_persistent_context(user_data_dir, headless=False)
                page = browser_context.pages[0] if browser_context.pages else browser_context.new_page()
                page.goto("https://www.instacart.com/store/walmart/storefront")
                log_to_web("Local browser launched. Please ensure you are logged in and on the Walmart storefront.", "warning")
            except Exception as pe:
                log_to_web(f"Failed to launch browser: {pe}", "error")
                current_session["running"] = False
                return

        if "instacart.com" not in page.url:
            log_to_web(f"Browser is on {page.url}, not Instacart. Directing to Walmart Storefront...", "info")
            try:
                page.goto("https://www.instacart.com/store/walmart/storefront")
            except Exception as pe:
                log_to_web(f"Navigation error: {pe}", "error")
                current_session["running"] = False
                return

        for idx, it in enumerate(items, 1):
            log_to_web(f"[{idx}/{len(items)}] Processing: {it['query']} (x{it['qty']})", "step")
            try:
                res = agent_core.add_item(page, it)
                results.append(res)
                current_session["results"] = results
                time.sleep(1.5)
            except Exception as e:
                log_to_web(f"Skipped '{it['query']}' ({type(e).__name__})", "error")
                results.append({"name": f"(error) {it['query']}", "price": None, "qty": 0, "status": "error"})
                current_session["results"] = results

    # Summarize & Save Order History
    grand = 0.0
    for r in results:
        line = (r["price"] or 0) * r["qty"]
        grand += line

    try:
        history_file = "order_history.json"
        history = []
        if os.path.exists(history_file):
            try:
                with open(history_file) as hf:
                    history = json.load(hf)
            except Exception:
                pass
        entry = {
            "date": time.strftime("%Y-%m-%d %H:%M:%S"),
            "total": grand,
            "items": [{"name": r["name"], "price": r["price"], "qty": r["qty"], "status": r["status"]} for r in results]
        }
        history.append(entry)
        with open(history_file, "w") as hf:
            json.dump(history, hf, indent=2)
        log_to_web("Logged transaction receipt summary to order_history.json", "success")
        
        try:
            import winsound
            winsound.MessageBeep(winsound.MB_OK)
        except Exception:
            pass
    except Exception as e:
        log_to_web(f"Failed to log order history: {e}", "warning")
        
    log_to_web("FINISHED", "finished")
    current_session["running"] = False

@app.route("/api/stream")
def sse_stream():
    def event_generator():
        while True:
            try:
                log_entry = current_session["logs"].get(timeout=10)
                yield f"data: {json.dumps(log_entry)}\n\n"
                if log_entry["status"] == "finished":
                    break
            except queue.Empty:
                yield "data: {\"message\": \"keep-alive\", \"status\": \"ping\"}\n\n"
    return Response(event_generator(), mimetype="text/event-stream")

@app.route("/api/results")
def get_results():
    return jsonify({"results": current_session["results"]})

# High-end glassmorphic UI Design
HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Instacart Walmart Grocery Agent</title>
    <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;600;800&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-color: #0b0f19;
            --card-bg: rgba(17, 25, 40, 0.75);
            --accent-green: #10b981;
            --accent-blue: #3b82f6;
            --accent-red: #ef4444;
            --text-color: #f3f4f6;
            --text-dim: #9ca3af;
            --border-color: rgba(255, 255, 255, 0.125);
        }
        
        * {
            box-sizing: border-box;
            margin: 0;
            padding: 0;
            font-family: 'Outfit', sans-serif;
        }
        
        body {
            background: radial-gradient(circle at top right, #1e293b, #0f172a, var(--bg-color));
            color: var(--text-color);
            min-height: 100vh;
            display: flex;
            flex-direction: column;
            align-items: center;
            padding: 2rem 1rem;
        }

        .container {
            width: 100%;
            max-width: 900px;
            backdrop-filter: blur(16px) saturate(180%);
            -webkit-backdrop-filter: blur(16px) saturate(180%);
            background-color: var(--card-bg);
            border-radius: 24px;
            border: 1px border var(--border-color);
            box-shadow: 0 8px 32px 0 rgba(0, 0, 0, 0.37);
            padding: 2.5rem;
            margin-bottom: 2rem;
        }

        h1 {
            font-size: 2.5rem;
            font-weight: 800;
            text-align: center;
            background: linear-gradient(to right, #10b981, #3b82f6);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            margin-bottom: 1.5rem;
        }

        .tabs {
            display: flex;
            gap: 1rem;
            margin-bottom: 2rem;
            border-bottom: 1px solid var(--border-color);
            padding-bottom: 1rem;
        }

        .tab-btn {
            background: none;
            border: none;
            color: var(--text-dim);
            font-size: 1.1rem;
            font-weight: 600;
            cursor: pointer;
            padding: 0.5rem 1rem;
            transition: all 0.3s;
            border-radius: 8px;
        }

        .tab-btn.active, .tab-btn:hover {
            color: var(--text-color);
            background: rgba(255, 255, 255, 0.05);
        }

        textarea {
            width: 100%;
            height: 150px;
            background: rgba(255, 255, 255, 0.03);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            color: var(--text-color);
            padding: 1rem;
            font-size: 1.1rem;
            margin-bottom: 1.5rem;
            resize: vertical;
            transition: all 0.3s;
        }

        textarea:focus {
            outline: none;
            border-color: var(--accent-blue);
            box-shadow: 0 0 10px rgba(59, 130, 246, 0.3);
        }

        .btn-row {
            display: flex;
            gap: 1rem;
            margin-bottom: 1.5rem;
        }

        button.primary-btn {
            flex: 1;
            background: linear-gradient(135deg, var(--accent-green), #059669);
            color: white;
            border: none;
            padding: 0.8rem 1.5rem;
            border-radius: 10px;
            font-size: 1.1rem;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.3s;
        }

        button.primary-btn:hover {
            opacity: 0.9;
            transform: translateY(-2px);
        }

        button.secondary-btn {
            background: rgba(255, 255, 255, 0.1);
            color: var(--text-color);
            border: 1px solid var(--border-color);
            padding: 0.8rem 1.5rem;
            border-radius: 10px;
            font-size: 1.1rem;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.3s;
        }

        button.secondary-btn:hover {
            background: rgba(255, 255, 255, 0.15);
        }

        .items-list {
            margin-top: 1.5rem;
        }

        .list-header {
            font-size: 1.3rem;
            font-weight: 600;
            margin-bottom: 1rem;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }

        .list-item {
            display: flex;
            align-items: center;
            justify-content: space-between;
            background: rgba(255, 255, 255, 0.02);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            padding: 0.8rem 1.2rem;
            margin-bottom: 0.6rem;
            transition: all 0.3s;
        }

        .list-item:hover {
            background: rgba(255, 255, 255, 0.05);
        }

        .item-info {
            display: flex;
            align-items: center;
            gap: 1rem;
        }

        .item-qty-input {
            width: 50px;
            background: rgba(0, 0, 0, 0.3);
            border: 1px solid var(--border-color);
            border-radius: 6px;
            color: var(--text-color);
            text-align: center;
            padding: 0.2rem;
            font-weight: 600;
        }

        .item-name {
            font-size: 1.1rem;
            font-weight: 600;
        }

        .item-size {
            color: var(--text-dim);
            font-size: 0.9rem;
        }

        .delete-btn {
            background: none;
            border: none;
            color: var(--accent-red);
            font-weight: bold;
            cursor: pointer;
            font-size: 1.2rem;
            padding: 0.2rem 0.5rem;
            border-radius: 4px;
        }

        .delete-btn:hover {
            background: rgba(239, 68, 68, 0.1);
        }

        .console-log {
            background: #000;
            font-family: monospace;
            padding: 1.5rem;
            border-radius: 12px;
            height: 250px;
            overflow-y: auto;
            margin-top: 1.5rem;
            border: 1px solid var(--border-color);
        }

        .log-entry {
            margin-bottom: 0.4rem;
            font-size: 0.95rem;
            line-height: 1.4;
        }

        .log-info { color: #888; }
        .log-success { color: var(--accent-green); font-weight: bold; }
        .log-warning { color: #fbbf24; }
        .log-error { color: var(--accent-red); font-weight: bold; }
        .log-step { color: #60a5fa; font-weight: bold; }

        .pantry-modal {
            display: none;
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background: rgba(0, 0, 0, 0.6);
            backdrop-filter: blur(4px);
            align-items: center;
            justify-content: center;
            z-index: 1000;
        }

        .pantry-content {
            background: #111827;
            border: 1px solid var(--border-color);
            border-radius: 18px;
            padding: 2rem;
            width: 90%;
            max-width: 600px;
        }

        .pantry-title {
            font-size: 1.5rem;
            font-weight: 600;
            margin-bottom: 1rem;
        }

        .pantry-textarea {
            height: 300px;
        }

        .add-input-row {
            display: flex;
            gap: 0.5rem;
            margin-top: 1rem;
        }

        .add-input {
            flex: 1;
            background: rgba(255, 255, 255, 0.03);
            border: 1px solid var(--border-color);
            border-radius: 8px;
            color: white;
            padding: 0.5rem 1rem;
        }

        .cart-summary {
            background: rgba(16, 185, 129, 0.05);
            border: 1px solid rgba(16, 185, 129, 0.2);
            border-radius: 12px;
            padding: 1.5rem;
            margin-top: 1.5rem;
        }

        .summary-total {
            font-size: 1.4rem;
            font-weight: bold;
            text-align: right;
            margin-top: 1rem;
            color: var(--accent-green);
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>🛒 Walmart Grocery Agent</h1>
        
        <div class="tabs">
            <button class="tab-btn active" id="tab-recipe" onclick="switchTab('recipe')">Describe Recipe</button>
            <button class="tab-btn" id="tab-paste" onclick="switchTab('paste')">Paste Shopping List</button>
            <button class="tab-btn" onclick="openPantry()">Manage Pantry</button>
            <button class="tab-btn" onclick="clearCache()">Clear Cache</button>
        </div>

        <div id="recipe-input-area">
            <textarea id="recipe-text" placeholder="What are we cooking? (e.g. Garlic chicken parmesan with fresh broccoli)"></textarea>
        </div>

        <div id="paste-input-area" style="display: none;">
            <textarea id="paste-text" placeholder="Paste your shopping list here (one item per line, e.g. 1 banana, 2 cans black beans)"></textarea>
        </div>

        <div class="btn-row">
            <button class="primary-btn" onclick="parseList()">Parse & Refine List</button>
        </div>

        <div class="items-list" id="items-list-section" style="display: none;">
            <div class="list-header">
                <span>📋 Grocery Items</span>
                <button class="primary-btn" style="max-width: 250px; background: linear-gradient(135deg, var(--accent-blue), #2563eb);" onclick="runAgent()">Send to Cart</button>
            </div>
            <div id="items-container"></div>
            
            <div class="add-input-row">
                <input class="add-input" id="new-item-input" placeholder="Add custom item to list...">
                <button class="secondary-btn" style="padding: 0.5rem 1rem;" onclick="addCustomItem()">Add</button>
            </div>
        </div>

        <div id="console-section" style="display: none;">
            <div class="list-header" style="margin-top: 1.5rem;">Console Progress Logs</div>
            <div class="console-log" id="console-log"></div>
        </div>

        <div class="cart-summary" id="cart-summary-section" style="display: none;">
            <div class="list-header">Receipt Cart Summary</div>
            <div id="receipt-container"></div>
            <div class="summary-total" id="receipt-total"></div>
        </div>
    </div>

    <!-- Pantry Modal -->
    <div class="pantry-modal" id="pantry-modal">
        <div class="pantry-content">
            <div class="pantry-title">Manage Pantry staples (will be auto-filtered)</div>
            <textarea class="pantry-textarea" id="pantry-text-area"></textarea>
            <div class="btn-row">
                <button class="primary-btn" onclick="savePantry()">Save Pantry</button>
                <button class="secondary-btn" onclick="closePantry()">Cancel</button>
            </div>
        </div>
    </div>

    <!-- Prompt User Modal -->
    <div class="pantry-modal" id="prompt-modal">
        <div class="pantry-content" style="max-width: 700px;">
            <div class="pantry-title" id="prompt-title">Which one?</div>
            <div class="list-header" id="prompt-subtitle" style="font-size: 1.1rem; color: var(--text-dim); margin-bottom: 1.5rem;"></div>
            <div id="prompt-options-container" style="display: flex; flex-direction: column; gap: 0.8rem; margin-bottom: 1.5rem;"></div>
            
            <div class="add-input-row" style="margin-bottom: 1.5rem;">
                <input class="add-input" id="prompt-custom-input" placeholder="Or type a custom query to search instead...">
                <button class="primary-btn" style="max-width: 120px; background: var(--accent-blue);" onclick="submitPromptSearch()">Search</button>
            </div>
            
            <div class="btn-row">
                <button class="secondary-btn" style="flex: 1;" onclick="submitPromptResponse('s')">Skip Item</button>
                <button class="secondary-btn" style="flex: 1;" onclick="submitPromptResponse('')">Accept Current Match</button>
            </div>
        </div>
    </div>

    <script>
        let currentMode = 'recipe';
        let groceryItems = [];

        function switchTab(mode) {
            currentMode = mode;
            document.getElementById('tab-recipe').classList.toggle('active', mode === 'recipe');
            document.getElementById('tab-paste').classList.toggle('active', mode === 'paste');
            document.getElementById('recipe-input-area').style.display = mode === 'recipe' ? 'block' : 'none';
            document.getElementById('paste-input-area').style.display = mode === 'paste' ? 'block' : 'none';
        }

        async function parseList() {
            const inputVal = currentMode === 'recipe' 
                ? document.getElementById('recipe-text').value 
                : document.getElementById('paste-text').value;

            if (!inputVal.trim()) {
                alert('Please input a recipe or list to parse.');
                return;
            }

            const response = await fetch('/api/parse', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ mode: currentMode, input: inputVal })
            });

            const data = await response.json();
            if (data.error) {
                alert('Parsing Error: ' + data.error);
                return;
            }

            groceryItems = data.items;
            renderItems();
        }

        function renderItems() {
            const container = document.getElementById('items-container');
            container.innerHTML = '';
            document.getElementById('items-list-section').style.display = 'block';

            groceryItems.forEach((item, idx) => {
                const itemDiv = document.createElement('div');
                itemDiv.className = 'list-item';
                
                const sz = item.size_str ? `<span class="item-size">[${item.size_str}]</span>` : '';
                itemDiv.innerHTML = `
                    <div class="item-info">
                        <input type="number" class="item-qty-input" value="${item.qty}" min="1" onchange="updateQty(${idx}, this.value)">
                        <span class="item-name">${item.query}</span>
                        ${sz}
                    </div>
                    <button class="delete-btn" onclick="deleteItem(${idx})">×</button>
                `;
                container.appendChild(itemDiv);
            });
        }

        function updateQty(idx, val) {
            groceryItems[idx].qty = Math.max(1, parseInt(val) || 1);
        }

        function deleteItem(idx) {
            groceryItems.splice(idx, 1);
            renderItems();
        }

        function addCustomItem() {
            const val = document.getElementById('new-item-input').value.trim();
            if (!val) return;
            
            // Send query to helper to extract quantity etc.
            groceryItems.push({
                query: val,
                qty: 1,
                size_str: "",
                req_oz: null,
                req_ct: null,
                req_fluid: false
            });
            
            document.getElementById('new-item-input').value = '';
            renderItems();
        }

        async function runAgent() {
            document.getElementById('console-section').style.display = 'block';
            const consoleLog = document.getElementById('console-log');
            consoleLog.innerHTML = '<div class="log-entry log-info">Initializing background agent...</div>';

            const response = await fetch('/api/run', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ items: groceryItems })
            });

            const data = await response.json();
            if (data.error) {
                consoleLog.innerHTML += `<div class="log-entry log-error">${data.error}</div>`;
                return;
            }

            // Listen to SSE logs
            const eventSource = new EventSource('/api/stream');
            eventSource.onmessage = function(event) {
                const log = JSON.parse(event.data);
                if (log.status === 'ping') return;
                
                if (log.status === 'finished') {
                    eventSource.close();
                    displayReceipt();
                    return;
                }
                
                if (log.status === 'input_required') {
                    showPrompt(log);
                }
                
                const entry = document.createElement('div');
                entry.className = `log-entry log-${log.status}`;
                entry.textContent = log.message;
                consoleLog.appendChild(entry);
                consoleLog.scrollTop = consoleLog.scrollHeight;
            };
        }

        async function displayReceipt() {
            setTimeout(async () => {
                document.getElementById('cart-summary-section').style.display = 'block';
                const container = document.getElementById('receipt-container');
                container.innerHTML = '';
                
                const response = await fetch('/api/results');
                const data = await response.json();
                
                let grandTotal = 0;
                data.results.forEach(r => {
                    const itemTotal = (r.price || 0) * r.qty;
                    grandTotal += itemTotal;
                    
                    const priceText = r.price !== null ? `$${r.price.toFixed(2)}` : 'N/A';
                    const totalText = r.price !== null ? `$${itemTotal.toFixed(2)}` : 'N/A';
                    
                    const itemDiv = document.createElement('div');
                    itemDiv.className = 'list-item';
                    
                    const statusClass = {
                        "added": "log-success",
                        "substituted": "log-warning",
                        "size-mismatch": "log-warning",
                        "bulk-pick": "log-warning",
                        "low-confidence": "log-warning",
                        "not-confirmed": "log-warning",
                        "not-carried": "log-error",
                        "error": "log-error",
                        "skipped": "log-info"
                    }[r.status] || "log-info";

                    const statusIcon = {
                        "added": "✓",
                        "substituted": "↻",
                        "size-mismatch": "↔",
                        "bulk-pick": "▣",
                        "low-confidence": "?",
                        "not-confirmed": "!",
                        "not-carried": "✗",
                        "error": "✗",
                        "skipped": "–"
                    }[r.status] || " ";

                    itemDiv.innerHTML = `
                        <div class="item-info">
                            <span class="${statusClass}">${statusIcon}</span>
                            <span class="item-name">${r.name}</span>
                        </div>
                        <div>
                            <span class="item-size">${r.qty} × ${priceText} = </span>
                            <span class="item-name" style="color: var(--accent-green); font-weight: 600;">${totalText}</span>
                        </div>
                    `;
                    container.appendChild(itemDiv);
                });
                
                document.getElementById('receipt-total').innerHTML = `ESTIMATED TOTAL: $${grandTotal.toFixed(2)}`;
            }, 1000);
        }

        function showPrompt(log) {
            document.getElementById('prompt-title').innerText = `Choose match for: "${log.query}"`;
            document.getElementById('prompt-subtitle').innerText = `Target qty: ${log.item.qty} · Size target: ${log.item.size_str || 'any'}`;
            
            const container = document.getElementById('prompt-options-container');
            container.innerHTML = '';
            
            log.options.forEach(opt => {
                const optDiv = document.createElement('div');
                optDiv.className = 'list-item';
                optDiv.style.cursor = 'pointer';
                optDiv.onclick = () => submitPromptResponse(opt.index.toString());
                
                const priceText = opt.price !== null ? `$${opt.price.toFixed(2)}` : 'N/A';
                
                optDiv.innerHTML = `
                    <div class="item-info">
                        <span class="log-step" style="font-weight: bold; margin-right: 0.5rem;">[${opt.index}]</span>
                        <span class="item-name">${opt.name}</span>
                    </div>
                    <div>
                        <span class="item-name" style="color: var(--accent-green); font-weight: 600;">${priceText}</span>
                    </div>
                `;
                container.appendChild(optDiv);
            });
            
            document.getElementById('prompt-custom-input').value = '';
            document.getElementById('prompt-modal').style.display = 'flex';
        }

        async function submitPromptResponse(resp) {
            document.getElementById('prompt-modal').style.display = 'none';
            await fetch('/api/input', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ response: resp })
            });
        }

        function submitPromptSearch() {
            const val = document.getElementById('prompt-custom-input').value.trim();
            if (!val) return;
            submitPromptResponse(val);
        }

        async function openPantry() {
            const response = await fetch('/api/pantry');
            const data = await response.json();
            document.getElementById('pantry-text-area').value = data.content;
            document.getElementById('pantry-modal').style.display = 'flex';
        }

        function closePantry() {
            document.getElementById('pantry-modal').style.display = 'none';
        }

        async function savePantry() {
            const content = document.getElementById('pantry-text-area').value;
            await fetch('/api/pantry', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ content: content })
            });
            closePantry();
        }

        async function clearCache() {
            const response = await fetch('/api/clear-cache', { method: 'POST' });
            const data = await response.json();
            alert(data.message);
        }
    </script>
</body>
</html>
"""

if __name__ == "__main__":
    def open_browser():
        time.sleep(1.5)
        try:
            chrome_path = "C:/Users/sherft/AppData/Local/Google/Chrome/Application/chrome.exe %s"
            webbrowser.get(chrome_path).open("http://127.0.0.1:5000")
        except Exception:
            webbrowser.open("http://127.0.0.1:5000")

    threading.Thread(target=open_browser, daemon=True).start()
    app.run(port=5000)

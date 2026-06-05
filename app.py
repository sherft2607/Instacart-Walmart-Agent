import os
import json
import time
import queue
import threading
import webbrowser
import sys
from flask import Flask, render_template_string, request, jsonify, Response
from playwright.sync_api import sync_playwright
import agent_core

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

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

import re

def log_to_web(msg, status="info"):
    # Strip ANSI escape sequences
    clean_msg = re.sub(r'\x1b\[[0-9;]*m', '', msg) if isinstance(msg, str) else msg
    current_session["logs"].put({"message": clean_msg, "status": status})

# Dynamic overrides for agent_core print statements
agent_core.info = lambda msg: log_to_web(msg, "info")
agent_core.ok = lambda msg: log_to_web(msg, "success")
agent_core.warn = lambda msg: log_to_web(msg, "warning")
agent_core.err = lambda msg: log_to_web(msg, "error")
agent_core.step = lambda n, total, name: log_to_web(f"[{n}/{total}] {name}", "step")

def web_get_user_input(options, used_q, it, chosen=None):
    serialized_options = []
    target_oz = (it["req_oz"] * it["qty"]) if it.get("req_oz") else None
    
    for idx, o in enumerate(options):
        # Calculate size string
        if o.get("size_oz") is not None:
            sz = agent_core.fmt_size(o["size_oz"], o.get("fluid", False))
        elif o.get("count"):
            sz = f"{o['count']} ct"
        else:
            sz = ""
            
        hint = ""
        need = 1
        if target_oz and o.get("size_oz"):
            need = max(1, round(target_oz / o["size_oz"]))
            actual_total_oz = need * o["size_oz"]
            hint = f"→ buy {need} for {agent_core.fmt_size(actual_total_oz, o.get('fluid', False))}"
            
        is_chosen = (chosen is not None and o.get("name") == chosen.get("name"))
        
        serialized_options.append({
            "index": idx + 1,
            "name": o["name"],
            "price": o["price"],
            "qty": need,
            "is_gv": o.get("is_gv", False),
            "bulk": o.get("bulk", False),
            "size_str": sz,
            "hint": hint,
            "is_chosen": is_chosen
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
    data = request.json or {}
    mode = data.get("mode", "recipe") # 'recipe' or 'paste'
    raw_input = data.get("input", "").strip()
    # Strip both raw and literal ANSI escape sequences (e.g. \x1b[1m or [1m)
    raw_input = re.sub(r'(?:\x1b)?\[[0-9;]*m', '', raw_input)
    
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
                res = agent_core.propose_item(page, it)
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

@app.route("/api/commit", methods=["POST"])
def commit_cart():
    data = request.json
    final_items = data.get("items", [])
    
    results = []
    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.connect_over_cdp(agent_core.CDP_URL)
                page = browser.contexts[0].pages[0] if browser.contexts[0].pages else browser.contexts[0].new_page()
            except Exception as e:
                print(f"CDP connect failed: {e}", flush=True)
                user_data_dir = os.path.join(os.getcwd(), "playwright_user_data")
                browser_context = p.chromium.launch_persistent_context(user_data_dir, headless=False)
                page = browser_context.pages[0] if browser_context.pages else browser_context.new_page()
            
            if "instacart.com" not in page.url:
                page.goto("https://www.instacart.com/store/walmart/storefront")

            for idx, it in enumerate(final_items, 1):
                try:
                    print(f"Processing commit for: {it['name']}", flush=True)
                    res = agent_core.commit_exact_item(page, it["name"], it["qty"])
                    results.append(res)
                    print(f"Commit result: {res}", flush=True)
                except Exception as e:
                    print(f"Error committing item {it['name']}: {e}", flush=True)
    except Exception as e:
        print(f"Thread crashed: {e}", flush=True)
        import traceback
        traceback.print_exc()
    print("Commit finished", flush=True)
    
    return jsonify({"status": "success", "results": results})

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
            --bg-color: #fcf1eb; /* warm neo-brutalism off-white */
            --card-bg: #ffffff;
            --accent-green: #4ade80;
            --accent-blue: #3b82f6;
            --accent-red: #f87171;
            --accent-yellow: #fde047;
            --accent-purple: #c084fc;
            --text-color: #0f172a;
            --text-dim: #475569;
            --border-color: #000000;
            --border-width: 3px;
            --card-shadow: 6px 6px 0px 0px rgba(0,0,0,1);
            --card-shadow-hover: 2px 2px 0px 0px rgba(0,0,0,1);
        }
        
        * {
            box-sizing: border-box;
            margin: 0;
            padding: 0;
            font-family: 'Outfit', sans-serif;
        }

        /* Premium Scrollbar - brutalist style */
        *::-webkit-scrollbar {
            width: 12px;
            height: 12px;
            border-left: 2px solid black;
        }
        *::-webkit-scrollbar-track {
            background: #ffffff;
        }
        *::-webkit-scrollbar-thumb {
            background: #000;
            border: 2px solid white;
        }
        
        body {
            background-color: var(--bg-color);
            color: var(--text-color);
            min-height: 100vh;
            display: flex;
            flex-direction: column;
            align-items: center;
            padding: 3rem 1rem;
            background-image: radial-gradient(#000000 1px, transparent 1px);
            background-size: 20px 20px;
        }

        .container {
            width: 100%;
            max-width: 950px;
            background-color: var(--card-bg);
            border-radius: 0;
            border: var(--border-width) solid var(--border-color);
            box-shadow: var(--card-shadow);
            padding: 3rem;
            margin-bottom: 2rem;
            position: relative;
        }

        h1 {
            font-size: 3.5rem;
            font-weight: 800;
            text-align: center;
            color: #000;
            margin-bottom: 2rem;
            text-transform: uppercase;
            letter-spacing: -1px;
            text-shadow: 4px 4px 0px var(--accent-yellow);
        }

        .tabs {
            display: flex;
            gap: 1rem;
            margin-bottom: 2.5rem;
            border-bottom: 3px solid #000;
            padding-bottom: 1rem;
            justify-content: center;
        }

        .tab-btn {
            background: #fff;
            border: var(--border-width) solid #000;
            color: #000;
            font-size: 1.1rem;
            font-weight: 800;
            cursor: pointer;
            padding: 0.6rem 1.2rem;
            transition: all 0.2s;
            box-shadow: 3px 3px 0px 0px #000;
            text-transform: uppercase;
        }

        .tab-btn.active {
            background: var(--accent-purple);
            box-shadow: 0px 0px 0px 0px #000;
            transform: translate(3px, 3px);
        }

        .tab-btn:hover:not(.active) {
            background: var(--accent-yellow);
        }

        textarea {
            width: 100%;
            height: 160px;
            background: #fff;
            border: var(--border-width) solid var(--border-color);
            color: var(--text-color);
            padding: 1.2rem;
            font-size: 1.2rem;
            font-weight: 600;
            line-height: 1.5;
            margin-bottom: 1.5rem;
            resize: vertical;
            transition: all 0.2s;
            box-shadow: inset 4px 4px 0px rgba(0,0,0,0.05);
        }

        textarea:focus {
            outline: none;
            background: var(--accent-yellow);
        }

        .btn-row {
            display: flex;
            gap: 1.5rem;
            margin-bottom: 2rem;
        }

        button.primary-btn {
            flex: 1;
            background: var(--accent-blue);
            color: white;
            border: var(--border-width) solid #000;
            padding: 1rem 2rem;
            font-size: 1.2rem;
            font-weight: 800;
            cursor: pointer;
            transition: all 0.2s;
            box-shadow: 4px 4px 0px 0px #000;
            text-transform: uppercase;
            letter-spacing: 1px;
        }

        button.primary-btn:hover {
            transform: translate(2px, 2px);
            box-shadow: 2px 2px 0px 0px #000;
        }

        button.primary-btn:active {
            transform: translate(4px, 4px);
            box-shadow: 0px 0px 0px 0px #000;
        }

        button.secondary-btn {
            background: #fff;
            color: #000;
            border: var(--border-width) solid #000;
            padding: 1rem 2rem;
            font-size: 1.1rem;
            font-weight: 800;
            cursor: pointer;
            transition: all 0.2s;
            box-shadow: 4px 4px 0px 0px #000;
            text-transform: uppercase;
        }

        button.secondary-btn:hover {
            background: var(--accent-yellow);
            transform: translate(2px, 2px);
            box-shadow: 2px 2px 0px 0px #000;
        }

        .items-list {
            margin-top: 2rem;
        }

        .list-header {
            font-size: 1.8rem;
            font-weight: 800;
            margin-bottom: 1.5rem;
            display: flex;
            justify-content: space-between;
            align-items: center;
            color: #000;
            text-transform: uppercase;
            border-bottom: 4px solid #000;
            padding-bottom: 0.5rem;
        }

        .list-item {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 1rem;
            background: #fff;
            border: var(--border-width) solid #000;
            padding: 1rem 1.5rem;
            margin-bottom: 0.8rem;
            transition: all 0.2s;
            box-shadow: 3px 3px 0px 0px #000;
        }

        .list-item:hover {
            background: #f8fafc;
            transform: translate(-2px, -2px);
            box-shadow: 5px 5px 0px 0px #000;
        }

        .item-info {
            display: flex;
            align-items: center;
            flex-wrap: wrap;
            gap: 0.8rem;
            flex: 1;
        }

        .item-qty-input {
            width: 60px;
            background: #fff;
            border: var(--border-width) solid #000;
            color: #000;
            text-align: center;
            padding: 0.4rem;
            font-size: 1.1rem;
            font-weight: 800;
        }

        .item-qty-input:focus {
            outline: none;
            background: var(--accent-yellow);
        }

        .item-name {
            font-size: 1.3rem;
            font-weight: 800;
        }

        .item-size {
            color: var(--text-dim);
            font-size: 1.1rem;
            font-weight: 600;
        }

        .delete-btn {
            background: #fff;
            border: var(--border-width) solid #000;
            color: #000;
            font-weight: 900;
            cursor: pointer;
            font-size: 1.4rem;
            width: 40px;
            height: 40px;
            display: flex;
            align-items: center;
            justify-content: center;
            transition: all 0.2s;
            box-shadow: 2px 2px 0px 0px #000;
        }

        .delete-btn:hover {
            background: var(--accent-red);
            color: white;
            transform: translate(2px, 2px);
            box-shadow: 0px 0px 0px 0px #000;
        }

        .status-panel {
            background: #fff;
            border: var(--border-width) solid #000;
            padding: 2rem;
            margin-top: 2rem;
            box-shadow: 6px 6px 0px 0px #000;
        }

        .active-item-card {
            background: var(--accent-yellow);
            border: var(--border-width) solid #000;
            padding: 1.5rem;
            margin-bottom: 2rem;
            display: flex;
            align-items: center;
            justify-content: space-between;
            box-shadow: 4px 4px 0px 0px #000;
        }

        .active-item-details {
            display: flex;
            flex-direction: column;
            gap: 0.5rem;
        }

        .active-item-title {
            font-size: 1.6rem;
            font-weight: 800;
            color: #000;
            text-transform: uppercase;
        }

        .active-item-status {
            font-size: 1.1rem;
            color: #000;
            display: flex;
            align-items: center;
            gap: 0.8rem;
            font-weight: 700;
        }

        .spinner {
            width: 24px;
            height: 24px;
            border: 4px solid #000;
            border-top-color: transparent;
            border-radius: 50%;
            animation: spin 0.8s linear infinite;
        }

        @keyframes spin {
            to { transform: rotate(360deg); }
        }

        .timeline {
            display: flex;
            flex-direction: column;
            gap: 1rem;
            max-height: 300px;
            overflow-y: auto;
            padding-right: 1rem;
        }

        .timeline-step {
            display: flex;
            align-items: flex-start;
            gap: 1rem;
            padding: 1rem 1.2rem;
            background: #fff;
            border: var(--border-width) solid #000;
            font-size: 1.1rem;
            font-weight: 600;
            box-shadow: 3px 3px 0px 0px #000;
            animation: slideIn 0.3s ease-out forwards;
        }

        @keyframes slideIn {
            from { opacity: 0; transform: translateY(10px) translateX(-10px); }
            to { opacity: 1; transform: translateY(0) translateX(0); }
        }

        .step-icon {
            display: flex;
            align-items: center;
            justify-content: center;
            width: 32px;
            height: 32px;
            font-size: 1.2rem;
            font-weight: 900;
            border: 2px solid #000;
            box-shadow: 2px 2px 0px 0px #000;
            flex-shrink: 0;
            background: #fff;
        }

        .step-success { background: var(--accent-green); color: #000; }
        .step-warning { background: var(--accent-yellow); color: #000; }
        .step-error { background: var(--accent-red); color: #000; }
        .step-info { background: #fff; color: #000; }
        .step-step { background: var(--accent-blue); color: white; }

        .pantry-modal {
            display: none;
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background: rgba(0, 0, 0, 0.8);
            align-items: center;
            justify-content: center;
            z-index: 1000;
        }

        .pantry-content {
            background: var(--bg-color);
            border: 4px solid #000;
            padding: 2.5rem;
            width: 90%;
            max-width: 700px;
            max-height: 90vh;
            display: flex;
            flex-direction: column;
            box-shadow: 12px 12px 0px 0px #000;
        }

        .pantry-title {
            font-size: 2rem;
            font-weight: 900;
            margin-bottom: 1.5rem;
            color: #000;
            text-transform: uppercase;
        }

        .pantry-textarea {
            height: 250px;
        }

        .add-input-row {
            display: flex;
            gap: 1rem;
            margin-top: 1.5rem;
        }

        .add-input {
            flex: 1;
            background: #fff;
            border: var(--border-width) solid #000;
            color: #000;
            padding: 0.8rem 1.2rem;
            font-size: 1.2rem;
            font-weight: 700;
            box-shadow: 3px 3px 0px 0px #000;
        }

        .add-input:focus {
            outline: none;
            background: var(--accent-yellow);
        }

        .cart-summary {
            background: var(--accent-purple);
            border: var(--border-width) solid #000;
            padding: 2rem;
            margin-top: 2.5rem;
            box-shadow: 6px 6px 0px 0px #000;
        }

        .summary-total {
            font-size: 2.5rem;
            font-weight: 900;
            text-align: right;
            margin-top: 1.5rem;
            color: #000;
            border-top: 4px solid #000;
            padding-top: 1.5rem;
        }
        
        /* Theme Toggle & Dark Mode for Brutalism */
        body.dark-mode {
            --bg-color: #0f172a;
            --card-bg: #1e293b;
            --text-color: #f8fafc;
            --text-dim: #cbd5e1;
            --border-color: #ffffff;
            --card-shadow: 6px 6px 0px 0px #fff;
            --card-shadow-hover: 2px 2px 0px 0px #fff;
            background-image: radial-gradient(rgba(255,255,255,0.2) 1px, transparent 1px);
        }
        
        body.dark-mode h1, body.dark-mode h2,
        body.dark-mode .list-header, 
        body.dark-mode .pantry-title, 
        body.dark-mode .active-item-title,
        body.dark-mode .active-item-status,
        body.dark-mode .summary-total,
        body.dark-mode .live-cart-title,
        body.dark-mode .live-cart-items,
        body.dark-mode .live-cart-total,
        body.dark-mode textarea {
            color: #fff;
        }

        body.dark-mode .container,
        body.dark-mode .list-item,
        body.dark-mode textarea,
        body.dark-mode .add-input,
        body.dark-mode .status-panel,
        body.dark-mode .timeline-step,
        body.dark-mode .step-icon,
        body.dark-mode .item-qty-input,
        body.dark-mode .delete-btn {
            background-color: var(--card-bg);
            border-color: #fff;
            box-shadow: 3px 3px 0px 0px #fff;
            color: #fff;
        }

        body.dark-mode .container { box-shadow: var(--card-shadow); }
        body.dark-mode .pantry-content { background-color: var(--card-bg); border-color: #fff; box-shadow: 12px 12px 0px 0px #fff; }
        
        body.dark-mode .tab-btn { background-color: var(--card-bg); border-color: #fff; color: #fff; box-shadow: 3px 3px 0px 0px #fff; }
        body.dark-mode .tab-btn:hover:not(.active) { background: var(--accent-yellow); color: #000; }
        body.dark-mode .tab-btn.active { background: var(--accent-purple); box-shadow: 0px 0px 0px 0px #fff; color: #000; }
        
        body.dark-mode .primary-btn { box-shadow: 4px 4px 0px 0px #fff; border-color: #fff; color: #000; }
        body.dark-mode .primary-btn:hover { box-shadow: 2px 2px 0px 0px #fff; }
        body.dark-mode .primary-btn:active { box-shadow: 0px 0px 0px 0px #fff; }
        
        body.dark-mode .secondary-btn { box-shadow: 4px 4px 0px 0px #fff; border-color: #fff; background: var(--card-bg); color: #fff; }
        body.dark-mode .secondary-btn:hover { box-shadow: 2px 2px 0px 0px #fff; background: var(--accent-yellow); color: #000; }
        
        body.dark-mode .delete-btn:hover { box-shadow: 0px 0px 0px 0px #fff; background: var(--accent-red); color: #fff; }
        body.dark-mode .list-item:hover { box-shadow: 5px 5px 0px 0px #fff; background: #334155; }
        
        body.dark-mode .list-header, body.dark-mode .tabs, body.dark-mode .live-cart-title { border-bottom-color: #fff; }
        body.dark-mode .summary-total, body.dark-mode .live-cart-total { border-top-color: #fff; }
        
        body.dark-mode .progress-container { border-color: #fff; box-shadow: 3px 3px 0px 0px #fff; background: var(--card-bg); }
        body.dark-mode .progress-bar { border-right-color: #fff; }
        
        .theme-toggle {
            position: absolute;
            top: 1.5rem;
            right: 1.5rem;
            background: #fff;
            border: var(--border-width) solid #000;
            color: #000;
            width: 48px; height: 48px;
            cursor: pointer;
            font-size: 1.5rem;
            display: flex; align-items: center; justify-content: center;
            box-shadow: 4px 4px 0px 0px #000;
            z-index: 10;
            transition: all 0.2s;
        }
        body.dark-mode .theme-toggle { background: var(--card-bg); border-color: #fff; box-shadow: 4px 4px 0px 0px #fff; }
        .theme-toggle:hover { transform: translate(2px,2px); box-shadow: 2px 2px 0px 0px #000; }
        body.dark-mode .theme-toggle:hover { box-shadow: 2px 2px 0px 0px #fff; }

        #live-cart-widget {
            position: fixed;
            top: 2rem;
            right: 2rem;
            width: 320px;
            background: var(--accent-green);
            border: var(--border-width) solid #000;
            padding: 1.5rem;
            box-shadow: 8px 8px 0px 0px #000;
            display: none;
            z-index: 50;
            color: #000;
        }
        .live-cart-title { font-size: 1.5rem; font-weight: 900; text-transform: uppercase; border-bottom: 3px solid #000; padding-bottom: 0.5rem; margin-bottom: 1rem; }
        .live-cart-items { max-height: 400px; overflow-y: auto; font-size: 1.1rem; font-weight: 700; }
        .live-cart-total { font-size: 1.5rem; font-weight: 900; margin-top: 1rem; border-top: 3px solid #000; padding-top: 1rem; text-align: right; }
        
        /* Progress Bar */
        .progress-container {
            width: 100%; height: 24px; background: #fff; border: 3px solid #000; margin-bottom: 1.5rem; overflow: hidden;
            display: none;
            box-shadow: 3px 3px 0px 0px #000;
        }
        .progress-bar {
            height: 100%; width: 0%; background: var(--accent-blue); border-right: 3px solid #000; transition: width 0.3s linear;
        } 
    </style>
    <script src="https://cdn.jsdelivr.net/npm/canvas-confetti@1.6.0/dist/confetti.browser.min.js"></script>
</head>
<body>
    <div class="container">
        <button class="theme-toggle" onclick="toggleTheme()">☀️</button>
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
            <div class="list-header" style="margin-top: 1.5rem;">
                <span>Cart Progress</span>
                <span id="progress-text" style="font-size: 1rem; color: var(--text-dim); font-weight: 500;"></span>
            </div>
            <div class="progress-container" id="progress-container">
                <div class="progress-bar" id="progress-bar"></div>
            </div>
            <div class="status-panel">
                <div class="active-item-card" id="active-item-card" style="display: none;">
                    <div class="active-item-details">
                        <div class="active-item-title" id="active-item-title">Processing Item</div>
                        <div class="active-item-status" id="active-item-status">
                            <div class="spinner"></div>
                            <span id="active-status-text">Searching...</span>
                        </div>
                    </div>
                </div>
                <div class="timeline" id="console-log"></div>
            </div>
        </div>

        <div class="cart-summary" id="cart-summary-section" style="display: none;">
            <div class="list-header">Receipt Cart Summary</div>
            <div id="receipt-container"></div>
            <div class="summary-total" id="receipt-total"></div>
        </div>
    </div>

    <!-- Live Cart Widget -->
    <div id="live-cart-widget">
        <div class="live-cart-title">Live Cart</div>
        <div class="live-cart-items" id="live-cart-items"></div>
        <div class="live-cart-total" id="live-cart-total">$0.00</div>
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
            <div id="prompt-options-container" style="display: flex; flex-direction: column; gap: 0.8rem; margin-bottom: 1.5rem; max-height: 50vh; overflow-y: auto; padding-right: 0.5rem;"></div>
            
            <div class="add-input-row" style="margin-bottom: 1.5rem;">
                <input class="add-input" id="prompt-custom-input" placeholder="Or type a custom query to search instead...">
                <button class="primary-btn" style="max-width: 120px; background: var(--accent-blue);" onclick="submitPromptSearch()">Search</button>
            </div>
            
            <div class="btn-row">
                <button class="secondary-btn" style="flex: 1;" onclick="submitPromptResponse('s')">Skip Item</button>
                <button class="secondary-btn" style="flex: 1;" id="btn-accept-current" onclick="submitPromptResponse('')">Accept Current Match</button>
            </div>
        </div>
    </div>

    <script>
        const audioCtx = new (window.AudioContext || window.webkitAudioContext)();
        
        const playTone = (freq, type, duration, vol=0.1) => {
            if(audioCtx.state === 'suspended') audioCtx.resume();
            const osc = audioCtx.createOscillator();
            const gain = audioCtx.createGain();
            osc.type = type;
            osc.frequency.setValueAtTime(freq, audioCtx.currentTime);
            gain.gain.setValueAtTime(vol, audioCtx.currentTime);
            gain.gain.exponentialRampToValueAtTime(0.001, audioCtx.currentTime + duration);
            osc.connect(gain);
            gain.connect(audioCtx.destination);
            osc.start();
            osc.stop(audioCtx.currentTime + duration);
        };

        const SoundFX = {
            success: () => {
                playTone(440, 'sine', 0.1);
                setTimeout(() => playTone(660, 'sine', 0.2), 100);
            },
            warning: () => playTone(220, 'triangle', 0.3, 0.15),
            error: () => {
                playTone(150, 'sawtooth', 0.2, 0.15);
                setTimeout(() => playTone(120, 'sawtooth', 0.3, 0.15), 150);
            },
            attention: () => {
                playTone(523.25, 'sine', 0.15);
                setTimeout(() => playTone(659.25, 'sine', 0.15), 150);
                setTimeout(() => playTone(783.99, 'sine', 0.3), 300);
            },
            finished: () => {
                playTone(523.25, 'sine', 0.2);
                setTimeout(() => playTone(659.25, 'sine', 0.2), 200);
                setTimeout(() => playTone(783.99, 'sine', 0.2), 400);
                setTimeout(() => playTone(1046.50, 'sine', 0.4), 600);
            }
        };

        let currentMode = 'recipe';
        let groceryItems = [];
        let totalItemsToProcess = 0;
        let itemsProcessed = 0;

        function toggleTheme() {
            document.body.classList.toggle('dark-mode');
            const btn = document.querySelector('.theme-toggle');
            btn.innerText = document.body.classList.contains('dark-mode') ? '☀️' : '🌙';
        }

        function switchTab(mode) {
            currentMode = mode;
            document.getElementById('tab-recipe').className = mode === 'recipe' ? 'tab-btn active' : 'tab-btn';
            document.getElementById('tab-paste').className = mode === 'paste' ? 'tab-btn active' : 'tab-btn';
            document.getElementById('recipe-input-area').style.display = mode === 'recipe' ? 'block' : 'none';
            document.getElementById('paste-input-area').style.display = mode === 'paste' ? 'block' : 'none';
        }

        async function parseList() {
            const text = currentMode === 'recipe' ? document.getElementById('recipe-text').value : document.getElementById('paste-text').value;
            if (!text.trim()) return alert("Please enter some text!");
            
            document.getElementById('items-list-section').style.display = 'block';
            document.getElementById('items-container').innerHTML = '<div class="list-item" style="justify-content:center"><div class="spinner"></div><span style="margin-left:1rem; color: var(--accent-blue);">Parsing items with AI...</span></div>';
            
            const response = await fetch('/api/parse', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ input: text, mode: currentMode })
            });
            const data = await response.json();
            groceryItems = data.items;
            renderItems();
        }

        function renderItems() {
            const container = document.getElementById('items-container');
            container.innerHTML = '';
            groceryItems.forEach((item, index) => {
                const itemDiv = document.createElement('div');
                itemDiv.className = 'list-item';
                
                const sizeText = item.size_str ? `<span class="item-size">(${item.size_str})</span>` : '';
                
                itemDiv.innerHTML = `
                    <div class="item-info">
                        <input type="number" class="item-qty-input" value="${item.qty}" min="1" onchange="updateQty(${index}, this.value)">
                        <span class="item-name">${item.query}</span>
                        ${sizeText}
                    </div>
                    <button class="delete-btn" onclick="deleteItem(${index})">×</button>
                `;
                container.appendChild(itemDiv);
            });
        }

        function updateQty(index, val) { groceryItems[index].qty = parseInt(val); }
        function deleteItem(index) { groceryItems.splice(index, 1); renderItems(); }

        function addCustomItem() {
            const val = document.getElementById('new-item-input').value.trim();
            if (!val) return;
            groceryItems.push({ query: val, qty: 1, size_str: null, req_oz: null, req_ct: null });
            document.getElementById('new-item-input').value = '';
            renderItems();
        }

        async function runAgent() {
            if (groceryItems.length === 0) return alert("List is empty!");
            
            document.getElementById('console-section').style.display = 'block';
            document.getElementById('console-log').innerHTML = '';
            document.getElementById('cart-summary-section').style.display = 'none';
            document.getElementById('live-cart-widget').style.display = 'block';
            document.getElementById('progress-container').style.display = 'block';
            
            totalItemsToProcess = groceryItems.length;
            itemsProcessed = 0;
            updateProgress();
            
            const response = await fetch('/api/run', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ items: groceryItems })
            });

            const data = await response.json();
            if (data.error) {
                document.getElementById('console-log').innerHTML += `<div class="timeline-step step-error"><div>${data.error}</div></div>`;
                return;
            }

            // Listen to SSE logs
            const eventSource = new EventSource('/api/stream');
            eventSource.onmessage = function(event) {
                const log = JSON.parse(event.data);
                if (log.status === 'ping') return;
                
                if (log.status === 'finished') {
                    SoundFX.finished();
                    confetti({ particleCount: 200, spread: 160, origin: { y: 0.3 }, zIndex: 9999, colors: ['#10b981', '#3b82f6', '#f59e0b', '#ef4444'] });
                    eventSource.close();
                    document.getElementById('active-item-card').style.display = 'none';
                    displayReceipt();
                    return;
                }
                
                if (log.status === 'input_required') {
                    SoundFX.attention();
                    showPrompt(log);
                }
                
                // If it's a step change, show/update the Active Item Card
                if (log.status === 'step') {
                    document.getElementById('active-item-card').style.display = 'flex';
                    document.getElementById('active-item-title').innerText = log.message;
                    document.getElementById('active-status-text').innerText = 'Adding to cart...';
                } else if (log.status === 'success' || log.status === 'error' || log.status === 'warning' || log.status === 'skipped') {
                    document.getElementById('active-status-text').innerText = log.message;
                    if (log.status === 'success') SoundFX.success();
                    if (log.status === 'warning') SoundFX.warning();
                    if (log.status === 'error') SoundFX.error();
                    
                    itemsProcessed++;
                    updateProgress();
                    updateLiveCart();
                }
                
                // Add to Timeline Feed
                const entry = document.createElement('div');
                entry.className = `timeline-step`;
                
                const statusClass = {
                    "success": "step-success",
                    "warning": "step-warning",
                    "error": "step-error",
                    "step": "step-step"
                }[log.status] || "step-info";

                const statusIcon = {
                    "success": "✓",
                    "warning": "⚠",
                    "error": "✗",
                    "step": "🔍"
                }[log.status] || "ℹ";

                entry.innerHTML = `
                    <div class="step-icon ${statusClass}">${statusIcon}</div>
                    <div>${log.message}</div>
                `;
                
                const consoleLog = document.getElementById('console-log');
                consoleLog.appendChild(entry);
                consoleLog.scrollTop = consoleLog.scrollHeight;
            };
        }

        function updateProgress() {
            const pct = totalItemsToProcess > 0 ? (itemsProcessed / totalItemsToProcess) * 100 : 0;
            document.getElementById('progress-bar').style.width = `${pct}%`;
            document.getElementById('progress-text').innerText = `${itemsProcessed} / ${totalItemsToProcess} items`;
        }

        async function updateLiveCart() {
            const response = await fetch('/api/results');
            const data = await response.json();
            
            let html = '';
            let total = 0;
            
            data.results.forEach(r => {
                if (r.status === 'error' || r.status === 'not-carried' || r.status === 'skipped') return;
                const cost = (r.price || 0) * r.qty;
                total += cost;
                html += `<div style="display:flex; justify-content:space-between; margin-bottom:0.5rem; padding-bottom:0.5rem; border-bottom:1px solid rgba(255,255,255,0.05)">
                            <div style="flex:1; padding-right:10px;">${r.qty}x ${r.name.substring(0,25)}...</div>
                            <div style="font-weight:600; color:var(--accent-green)">$${cost.toFixed(2)}</div>
                         </div>`;
            });
            document.getElementById('live-cart-items').innerHTML = html;
            document.getElementById('live-cart-total').innerText = `Total: $${total.toFixed(2)}`;
        }

        async function displayReceipt() {
            document.getElementById('live-cart-widget').style.display = 'none';
            setTimeout(async () => {
                document.getElementById('cart-summary-section').style.display = 'block';
                const container = document.getElementById('receipt-container');
                container.innerHTML = '';
                
                const response = await fetch('/api/results');
                const data = await response.json();
                
                // Save to global variable for swapping
                window.proposedCart = data.results;
                
                renderProposedCart();
            }, 1000);
        }

        function renderProposedCart() {
            const container = document.getElementById('receipt-container');
            container.innerHTML = '';
            
            let grandTotal = 0;
            window.proposedCart.forEach((r, idx) => {
                const itemTotal = (r.price || 0) * r.qty;
                grandTotal += itemTotal;
                
                const priceText = r.price !== null ? `$${r.price.toFixed(2)}` : 'N/A';
                const totalText = r.price !== null ? `$${itemTotal.toFixed(2)}` : 'N/A';
                
                const itemDiv = document.createElement('div');
                itemDiv.className = 'list-item';
                itemDiv.style.display = 'flex';
                itemDiv.style.flexDirection = 'column';
                itemDiv.style.border = '3px solid black';
                itemDiv.style.marginBottom = '1rem';
                itemDiv.style.padding = '1rem';
                itemDiv.style.background = 'white';
                
                let optionsHtml = '';
                if (r.options && r.options.length > 0) {
                    optionsHtml = `<div style="margin-top:1rem; border-top:2px dashed black; padding-top:1rem; display:none;" id="options-${idx}">
                        <p style="font-weight:bold; margin-bottom:0.5rem;">Alternative Options:</p>
                        ${r.options.map(opt => `
                            <div style="padding:0.5rem; border:2px solid black; margin-bottom:0.5rem; cursor:pointer; background:#F4F4F0;" onclick="swapItem(${idx}, '${opt.name.replace(/'/g, "\\'")}', ${opt.price}, ${opt.qty})">
                                ${opt.name} - ${opt.qty} × $${opt.price} = <span style="font-weight:bold;">$${(opt.price*opt.qty).toFixed(2)}</span>
                            </div>
                        `).join('')}
                    </div>
                    <button onclick="document.getElementById('options-${idx}').style.display='block'" style="margin-top:0.5rem; background:#FACC15; border:3px solid black; padding:0.5rem; font-weight:bold; cursor:pointer;">SWAP ITEM</button>
                    `;
                }

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
                
                const statusBadge = (r.status && r.status !== 'added') ? `<span style="margin-left:10px; font-size:0.75rem; background:#FACC15; padding:2px 6px; border:2px solid black;">${r.status.toUpperCase()}</span>` : '';

                itemDiv.innerHTML = `
                    <div style="margin-bottom:0.5rem; color: #666; font-family: monospace; font-size: 0.9rem; display:flex; align-items:center;">
                        <span class="${statusClass}" style="margin-right:10px; font-size:1.2rem;">${statusIcon}</span>
                        Recipe Item: <strong>"${r.query || 'Unknown item'}"</strong>
                    </div>
                    <div style="display:flex; justify-content:space-between; align-items:center;">
                        <div class="item-info">
                            <span class="item-name" style="font-weight:bold;">${r.name}</span>
                            ${statusBadge}
                        </div>
                        <div>
                            <span class="item-size">${r.qty} × ${priceText} = </span>
                            <span class="item-name" style="background:#4ADE80; padding:0.2rem 0.5rem; border:2px solid black; font-weight: 900;">${totalText}</span>
                        </div>
                    </div>
                    ${optionsHtml}
                `;
                container.appendChild(itemDiv);
            });
            
            const btnHtml = `
            <div id="commit-btn-div" style="margin-top:2rem; text-align:center;">
                <button onclick="commitCart()" style="background:#4ADE80; color:black; border:4px solid black; box-shadow: 6px 6px 0px 0px black; padding:1.5rem 3rem; font-size:1.5rem; font-weight:900; cursor:pointer; text-transform:uppercase;">CONFIRM & SEND TO INSTACART</button>
            </div>
            `;
            container.insertAdjacentHTML('beforeend', btnHtml);
            
            document.getElementById('receipt-total').innerHTML = `PROPOSED TOTAL: $${grandTotal.toFixed(2)}`;
        }

        function swapItem(index, newName, newPrice, newQty) {
            window.proposedCart[index].name = newName;
            window.proposedCart[index].price = newPrice;
            window.proposedCart[index].qty = newQty;
            window.proposedCart[index].options = []; // clear options after swap
            renderProposedCart();
        }

        async function commitCart() {
            const btnDiv = document.getElementById('commit-btn-div');
            if (btnDiv) btnDiv.innerHTML = '<h2 style="text-align:center;">COMMITTING TO CART... (Please wait up to 30 seconds)</h2>';
            
            try {
                const response = await fetch('/api/commit', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ items: window.proposedCart })
                });
                const data = await response.json();
                if (data.status === "success") {
                    if (btnDiv) btnDiv.innerHTML = '<h2 style="text-align:center; color:#4ADE80; text-shadow: 2px 2px 0px black;">SUCCESS! ITEMS ADDED TO WALMART CART.</h2>';
                    document.getElementById('receipt-total').innerHTML = document.getElementById('receipt-total').innerHTML.replace("PROPOSED TOTAL", "FINAL TOTAL");
                } else {
                    if (btnDiv) btnDiv.innerHTML = '<h2 style="text-align:center; color:#F87171; text-shadow: 2px 2px 0px black;">ERROR COMMITTING CART</h2>';
                }
            } catch (e) {
                if (btnDiv) btnDiv.innerHTML = '<h2 style="text-align:center; color:#F87171; text-shadow: 2px 2px 0px black;">ERROR COMMITTING CART</h2>';
            }
        }

        function showPrompt(log) {
            document.getElementById('prompt-title').innerText = `Choose match for: "${log.item.query}"`;
            
            let subtitle = `Target qty: ${log.item.qty} · Size target: ${log.item.size_str || 'any'}`;
            if (log.query.toLowerCase() !== log.item.query.toLowerCase()) {
                subtitle += ` · searched fallback: "${log.query}"`;
            }
            document.getElementById('prompt-subtitle').innerText = subtitle;
            
            const container = document.getElementById('prompt-options-container');
            container.innerHTML = '';
            
            let chosenName = 'None';
            log.options.forEach(opt => {
                const optDiv = document.createElement('div');
                optDiv.className = 'list-item';
                optDiv.style.cursor = 'pointer';
                optDiv.onclick = () => submitPromptResponse(opt.index.toString());
                
                if (opt.is_chosen) {
                    optDiv.style.border = '1px solid var(--accent-green)';
                    optDiv.style.background = 'rgba(16, 185, 129, 0.08)';
                    chosenName = opt.name;
                }
                
                const qty = opt.qty || 1;
                const totalPrice = opt.price !== null ? (opt.price * qty) : null;
                const priceText = totalPrice !== null ? `$${totalPrice.toFixed(2)}` : 'N/A';
                
                const badge = opt.is_chosen ? `<span style="background: var(--accent-green); color: white; font-size: 0.75rem; padding: 0.1rem 0.4rem; border-radius: 4px; font-weight: bold; margin-left: 0.5rem;">CURRENT MATCH</span>` : '';
                const bulkBadge = opt.bulk ? `<span style="background: var(--accent-red); color: white; font-size: 0.75rem; padding: 0.1rem 0.4rem; border-radius: 4px; font-weight: bold; margin-left: 0.5rem;">BULK</span>` : '';
                const sizeBadge = opt.size_str ? `<span style="color: var(--text-dim); margin-left: 0.5rem; font-size: 0.95rem;">${opt.size_str}</span>` : '';
                const hintText = opt.hint ? `<span style="color: var(--text-dim); margin-left: 0.5rem; font-style: italic; font-size: 0.95rem;">${opt.hint}</span>` : '';
                
                optDiv.innerHTML = `
                    <div class="item-info">
                        <span class="log-step" style="font-weight: bold; margin-right: 0.5rem;">[${opt.index}]</span>
                        <span class="item-name">${opt.name}</span>
                        ${sizeBadge}
                        ${bulkBadge}
                        ${badge}
                        ${hintText}
                    </div>
                    <div style="text-align: right;">
                        <div class="item-name" style="color: var(--accent-green); font-weight: 600;">${priceText}</div>
                        ${qty > 1 && opt.price !== null ? `<div style="font-size: 0.8rem; color: var(--text-dim);">${qty} × $${opt.price.toFixed(2)}</div>` : ''}
                    </div>
                `;
                container.appendChild(optDiv);
            });
            
            document.getElementById('btn-accept-current').innerText = `ACCEPT CURRENT MATCH`;
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

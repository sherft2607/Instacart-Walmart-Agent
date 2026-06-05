import os
import time
import json
from playwright.sync_api import sync_playwright
from agent_core import (
    C, banner, step, ok, warn, err, info,
    read_pasted_list, get_grocery_list, filter_pantry,
    review_and_edit_list, add_item, CDP_URL, CACHE_FILE
)

banner("🛒  Walmart Grocery Agent")
print(f"\n{C.BOLD}How would you like to start?{C.RESET}")
print(f"   {C.BOLD}1{C.RESET}  Paste a shopping list")
print(f"   {C.BOLD}2{C.RESET}  Describe a recipe")
print(f"   {C.BOLD}3{C.RESET}  Clear search cache")
mode = input(f"   {C.CYAN}▸ {C.RESET}").strip()

if mode == "3":
    if os.path.exists(CACHE_FILE):
        try:
            os.remove(CACHE_FILE)
            ok("Cache file cleared successfully.")
        except Exception as e:
            warn(f"Failed to clear cache: {e}")
    else:
        info("No cache file found to clear.")
    print(f"\n{C.BOLD}How would you like to start?{C.RESET}")
    print(f"   {C.BOLD}1{C.RESET}  Paste a shopping list")
    print(f"   {C.BOLD}2{C.RESET}  Describe a recipe")
    mode = input(f"   {C.CYAN}▸ {C.RESET}").strip()

items = read_pasted_list() if mode == "1" else get_grocery_list(input(f"   {C.CYAN}What are we cooking? ▸ {C.RESET}"))
items = filter_pantry(items)
items = review_and_edit_list(items)

results = []
with sync_playwright() as p:
    try:
        browser = p.chromium.connect_over_cdp(CDP_URL)
        page = browser.contexts[0].pages[0] if browser.contexts[0].pages else browser.contexts[0].new_page()
    except Exception as e:
        warn(f"Could not connect to Chrome debugging port at {CDP_URL} ({type(e).__name__})")
        print(f"\n   {C.BOLD}To run with remote Chrome debugging, close all Chrome windows and launch it via:{C.RESET}")
        print(f"     Windows: {C.GREEN}chrome.exe --remote-debugging-port=9223 --user-data-dir=\"%LOCALAPPDATA%\\Google\\Chrome\\User Data\\Dev\"{C.RESET}")
        print(f"     macOS:   {C.GREEN}/Applications/Google\\ Chrome.app/Contents/MacOS/Google\\ Chrome --remote-debugging-port=9223 --user-data-dir=\"/tmp/chrome_dev\"{C.RESET}")
        info("Launching a local Chromium instance with persistent session context...")
        user_data_dir = os.path.join(os.getcwd(), "playwright_user_data")
        try:
            browser_context = p.chromium.launch_persistent_context(user_data_dir, headless=False)
        except Exception as pe:
            if "Executable doesn't exist" in str(pe) or "Playwright" in str(pe):
                err("Playwright browser binaries are missing!")
                print(f"   Run: {C.GREEN}playwright install chromium{C.RESET} to install them.")
                raise SystemExit
            raise pe
        page = browser_context.pages[0] if browser_context.pages else browser_context.new_page()
        page.goto("https://www.instacart.com/store/walmart/storefront")
        print(f"\n{C.BOLD}Please log in to Instacart and navigate to the Walmart store (if not already there).{C.RESET}")
        input(f"   {C.CYAN}▸ Press Enter once the page is ready...{C.RESET}")

    if "instacart.com" not in page.url:
        err(f"Browser is on {page.url}, not Instacart. Open the Walmart storefront, then re-run.")
        raise SystemExit
    for idx, it in enumerate(items, 1):
        step(idx, len(items), f"{it['query']} {C.DIM}(×{it['qty']}){C.RESET}")
        try:
            results.append(add_item(page, it))
            time.sleep(2)
        except Exception as e:
            err(f"Skipped '{it['query']}' ({type(e).__name__})")
            results.append({"name": f"(error) {it['query']}", "price": None, "qty": 0, "status": "error"})

banner("🧾  Cart Summary")
grand = 0.0
for r in results:
    line = (r["price"] or 0) * r["qty"]
    grand += line
    color = {"added": C.GREEN, "substituted": C.YELLOW, "size-mismatch": C.YELLOW,
             "bulk-pick": C.YELLOW, "low-confidence": C.YELLOW, "not-confirmed": C.YELLOW,
             "not-carried": C.RED, "error": C.RED, "skipped": C.GRAY}.get(r["status"], "")
    icon = {"added": "✓", "substituted": "↻", "size-mismatch": "↔", "bulk-pick": "▣",
            "low-confidence": "?", "not-confirmed": "!", "not-carried": "✗",
            "error": "✗", "skipped": "–"}.get(r["status"], " ")
    name = (r["name"][:38] + "…") if len(r["name"]) > 39 else r["name"]
    ps = f"${line:6.2f}" if r["price"] is not None else "   —  "
    print(f" {color}{icon}{C.RESET} {name:40}{C.DIM}{r['qty']}×{C.RESET}  {C.BOLD}{ps}{C.RESET}")
print(f" {C.GRAY}{'─' * 58}{C.RESET}")
print(f" {C.BOLD}TOTAL{C.RESET}{'':37}{C.GREEN}{C.BOLD}${grand:7.2f}{C.RESET}")
print(f" {C.GRAY}{'─' * 58}{C.RESET}")
print(f" {C.GRAY}✓ added  ↻ brand swap  ↔ size  ▣ bulk  ? check  ✗ missing  – skipped{C.RESET}\n")

# Save to order history log
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
    ok(f"Logged transaction summary to {history_file}")
    try:
        import winsound
        winsound.MessageBeep(winsound.MB_OK)
    except Exception:
        pass
except Exception as e:
    warn(f"Failed to log order history: {e}")

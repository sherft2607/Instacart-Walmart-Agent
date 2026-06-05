import os
import re
import json
import time
import hashlib
from google import genai
from google.genai import errors
from google.genai.types import GenerateContentConfig
from playwright.sync_api import sync_playwright

client = genai.Client()

CDP_URL = "http://localhost:9222"
PREFER_BRAND = True
MANUAL_OVERRIDE = True
CACHE_FILE = "grocery_cache.json"
SEARCH_WAIT = 2.5
MAX_TILES = 15
SIZE_TOLERANCE = 0.35

# ---------------- UI / STYLING ----------------
class C:
    RESET = "\033[0m"; BOLD = "\033[1m"; DIM = "\033[2m"
    GREEN = "\033[92m"; YELLOW = "\033[93m"; RED = "\033[91m"
    BLUE = "\033[94m"; CYAN = "\033[96m"; GRAY = "\033[90m"

os.system("")  # enable ANSI colors on Windows

def banner(text):
    line = "═" * (len(text) + 4)
    print(f"\n{C.CYAN}╔{line}╗{C.RESET}")
    print(f"{C.CYAN}║  {C.BOLD}{text}{C.RESET}{C.CYAN}  ║{C.RESET}")
    print(f"{C.CYAN}╚{line}╝{C.RESET}")

def step(n, total, name):
    print(f"\n{C.GRAY}[{n}/{total}]{C.RESET} {C.BOLD}🔍 {name}{C.RESET}")

def ok(msg):   print(f"   {C.GREEN}✓{C.RESET} {msg}")
def warn(msg): print(f"   {C.YELLOW}⚠ {msg}{C.RESET}")
def err(msg):  print(f"   {C.RED}✗ {msg}{C.RESET}")
def info(msg): print(f"   {C.GRAY}{msg}{C.RESET}")

system_instruction = (
    "You turn a recipe into a grocery shopping list for Instacart's Walmart storefront. "
    "List only main ingredients (skip salt, pepper, water, basic spices). Use 'Great Value' "
    "unless a specific brand matters. Substitute niche/specialty items for the closest common "
    "item Walmart stocks. Prepend 'Fresh' for raw produce only (never eggs/dairy/meat). "
    "CRITICAL: 'qty' must be the number of STORE PACKAGES to buy (usually 1). "
    "Do NOT put recipe measurements (like 10 from '10 oz') into 'qty'. Instead, put the measurement into 'size' "
    "(e.g., qty: 1, size: '10 oz'). Leave 'size' blank for things like sauces or produce where size doesn't matter. "
    'Output ONLY a JSON array of {"query","qty","size"}.'
)

BRAND_WORDS = {"great", "value"}
GENERIC_LEADERS = {
    "fresh", "organic", "large", "small", "whole", "raw", "natural", "pure", "mild",
    "medium", "hot", "less", "low", "premium", "superior", "gluten", "free", "all",
    "boneless", "skinless", "finely", "shredded",
}
GENERIC_HEADS = {"pasta", "cheese", "sauce", "mix", "seasoning", "oil", "broth",
                 "blend", "noodles", "noodle", "beans", "rice"}
EXCLUDE_TERMS = {"seeds", "gardener", "for planting", "onion sets", "plant sets",
                 "sapling", "delivery fee", "free delivery", "membership", "ebt",
                 "baby powder", "body powder", "shampoo", "lotion", "deodorant",
                 "detergent", "soap", "diaper", "wipes", "supplement", "vitamin",
                 "cosmetic", "makeup", "pet ", "dog ", "cat ", "litter"}
PRECUT_TERMS = {"diced", "minced", "chopped", "sliced", "cubed"}
FLUID_HINTS = {"broth", "stock", "oil", "vinegar", "juice", "milk", "water",
               "drink", "beverage", "creamer", "sauce", "cream"}
BULK_TERMS = {"bag", "bulk", "case", "club", "value pack", "family pack"}
WORD_NUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
            "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
CONTAINERS = r"(?:cans?|bags?|jars?|boxes?|bottles?|packs?|cartons?|tubs?|bunch(?:es)?|crowns?)"

RE_SIZE = re.compile(r"(\d+(?:\.\d+)?)\s*(fl\s*oz|oz|ounces?|lbs?|pounds?|pints?|quarts?|qts?|gallons?|gals?|grams?|g|ml|milliliters?|pt|pts)\b", re.I)
RE_COUNT = re.compile(r"(\d+)\s*(?:ct|count)\b", re.I)

def parse_size(text):
    m = RE_SIZE.search(text.lower())
    if not m:
        return None
    val, unit = float(m.group(1)), m.group(2).replace(" ", "")
    if unit in ("lb", "lbs", "pound", "pounds"):
        return val * 16
    if unit in ("pint", "pints", "pt", "pts"):
        return val * 16
    if unit in ("quart", "quarts", "qt", "qts"):
        return val * 32
    if unit in ("gallon", "gallons", "gal", "gals"):
        return val * 128
    return val

def is_fluid(text):
    return "fl oz" in text.lower()

def parse_count(text):
    m = RE_COUNT.search(text.lower())
    return int(m.group(1)) if m else None

def fmt_size(v, fluid=False):
    if v is None:
        return "?"
    if not fluid and v >= 16:
        return (f"{v / 16:.2f}".rstrip("0").rstrip(".")) + " lb"
    unit = "fl oz" if fluid else "oz"
    return (f"{v:.1f}".rstrip("0").rstrip(".")) + " " + unit

def parse_qty(text):
    t = text.lower().strip()
    m_size = re.match(r"^(\d+(?:\.\d+)?)\s*(?:fl\s*oz|oz|lbs?|pounds?|pints?|quarts?|qts?|gallons?|gals?|grams?|g|ml|milliliters?|pt|pts)\b", t)
    if m_size:
        return 1
    m = re.match(r"^(\d+)\s+", t)
    if m:
        return int(m.group(1))
    for w, n in WORD_NUM.items():
        if re.match(rf"^{w}\b", t):
            return n
    m = re.search(rf"\b(\d+)\s+{CONTAINERS}\b", t)
    return int(m.group(1)) if m else 1

def clean_query(line):
    q = line
    q = re.sub(r"^\s*\d+\s+", "", q)
    for w in WORD_NUM:
        q = re.sub(rf"^\s*{w}\b\s*", "", q, flags=re.I)
    q = re.sub(rf"\b\d*\s*{CONTAINERS}\s+of\b", "", q, flags=re.I)
    q = re.sub(rf"\b{CONTAINERS}\s+of\b", "", q, flags=re.I)
    q = re.sub(r"\b\d+(?:\.\d+)?\s*(?:fl\s*oz|oz|ounces?|lbs?|pounds?)\b", "", q, flags=re.I)
    q = re.sub(r"\b\d+\s*(?:ct|count)\b", "", q, flags=re.I)
    return re.sub(r"\s{2,}", " ", q).strip(" ,-")

def parse_pasted_line(line):
    size_str = ""
    m = re.search(r"\d+(?:\.\d+)?\s*(?:fl\s*oz|oz|lbs?|pounds?|ct|count)", line.lower())
    if m:
        size_str = m.group(0)
    return {"query": clean_query(line), "qty": parse_qty(line),
            "req_oz": parse_size(line), "req_ct": parse_count(line),
            "req_fluid": is_fluid(line), "size_str": size_str}

def generate_with_retry(**kwargs):
    for _ in range(2):
        try:
            return client.models.generate_content(**kwargs)
        except errors.ClientError as e:
            if getattr(e, "code", None) == 429:
                raise RuntimeError("Gemini quota hit. Wait until midnight PT or enable billing.") from e
            raise

def get_grocery_list(user_input):
    key = hashlib.md5(user_input.lower().strip().encode()).hexdigest()
    cache = {}
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE) as f:
                cache = json.load(f)
        except (json.JSONDecodeError, ValueError):
            warn("Cache file was empty/corrupt — ignoring it.")
    if key in cache:
        info("Using cached list (no API call).")
        return cache[key]
    resp = generate_with_retry(
        model="gemini-2.5-flash", contents=user_input,
        config=GenerateContentConfig(system_instruction=system_instruction, temperature=0.1),
    )
    raw = re.sub(r"^```(json)?|```$", "", resp.text.strip(), flags=re.MULTILINE).strip()
    items = []
    for e in json.loads(raw):
        q = str(e.get("query", "")).strip()
        if not q:
            continue
        size = str(e.get("size", "")).strip()
        items.append({"query": q, "qty": max(1, int(e.get("qty", 1) or 1)),
                      "req_oz": parse_size(size), "req_ct": parse_count(size),
                      "req_fluid": is_fluid(size), "size_str": size})
    cache[key] = items
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f)
    return items

def stem(w):
    return w[:-1] if len(w) > 3 and w.endswith("s") else w

def pick_head(descriptors):
    for w in reversed(descriptors):
        if w not in GENERIC_HEADS:
            return w
    return descriptors[-1] if descriptors else None

def find_search_box(page):
    return page.locator('input[aria-label="Search"], input[placeholder^="Search Walmart"]').first

def handle_ripeness(page):
    try:
        dialog = page.locator('[role="dialog"]').first
        if not (dialog.count() and dialog.is_visible()):
            return
        ripe = dialog.locator('text=/ready to eat/i')
        if ripe.count():
            ripe.first.click(timeout=2000); time.sleep(0.6)
        else:
            exact = dialog.get_by_text(re.compile(r"^\s*Ripe\s*$", re.I))
            if exact.count():
                exact.first.click(timeout=2000); time.sleep(0.6)
        for label in ["Save", "Confirm", "Done", "Add", "Continue"]:
            btn = dialog.locator(f'button:has-text("{label}")')
            if btn.count() and btn.first.is_visible():
                btn.first.click(timeout=2000); time.sleep(0.6); return
        page.keyboard.press("Escape"); time.sleep(0.4)
    except Exception:
        pass

def search_item(page, item):
    handle_ripeness(page)
    box = find_search_box(page)
    box.click(); box.press("Control+A"); box.press("Delete")
    box.type(item, delay=45); box.press("Enter")
    time.sleep(SEARCH_WAIT)

def product_name_from_label(label):
    m = re.match(r"^Add\s+\d+\s+\S+\s+(.*)$", label)
    return m.group(1).strip() if m else label.replace("Add", "", 1).strip()

def card_text(button):
    for xp in ["xpath=ancestor::li[1]", "xpath=ancestor::div[3]",
               "xpath=ancestor::div[5]", "xpath=ancestor::div[7]"]:
        try:
            t = button.locator(xp).first.inner_text()
            if "$" in t:
                return t
        except Exception:
            pass
    return ""

def extract_price(text):
    m = re.search(r"Current price:\s*\$(\d+\.\d{2})", text)
    if m:
        return float(m.group(1))
    m = re.search(r"\$(\d+\.\d{2})", text)
    return float(m.group(1)) if m else None

def query_brand_tokens(item):
    toks = [w for w in re.findall(r"[a-z]+", item.lower()) if len(w) > 2]
    brand = []
    for t in toks:
        if t in GENERIC_LEADERS:
            if brand:
                break
            continue
        brand.append(t)
        if len(brand) >= 3:
            break
    return brand

def broaden(query):
    words = [w for w in query.split() if w.lower() not in BRAND_WORDS
             and w.lower() not in GENERIC_LEADERS]
    return " ".join(words) if words else query

def _collect(page, query):
    q_lower = query.lower()
    produce = q_lower.startswith("fresh ")
    q_has_precut = any(p in q_lower for p in PRECUT_TERMS)
    words = [w for w in re.findall(r"[a-z]+", q_lower) if len(w) > 2]
    descriptors = [stem(w) for w in words if w not in BRAND_WORDS]
    head_noun = pick_head(descriptors)

    buttons = page.locator('button[aria-label^="Add"]')
    cands = []
    for i in range(min(buttons.count(), MAX_TILES)):
        b = buttons.nth(i)
        try:
            if not b.is_visible():
                continue
            raw = b.get_attribute("aria-label") or ""
            label = raw.lower()
            if "favorite" in label or any(x in label for x in EXCLUDE_TERMS):
                continue
            name = product_name_from_label(raw)
            if name.startswith("$") or "delivery fee" in name.lower() or len(name) < 3:
                continue
            if produce and not q_has_precut and any(p in label for p in PRECUT_TERMS):
                continue
            matches = sum(1 for w in descriptors if w in label)
            head_ok = bool(head_noun and head_noun in label)
            score = matches + (1 if head_ok else 0)
            cands.append({"btn": b, "name": name, "type_score": score,
                          "matches": matches, "head_ok": head_ok,
                          "is_gv": "great value" in label,
                          "bulk_word": any(t in label for t in BULK_TERMS)})
        except Exception:
            continue
    if not cands:
        return []
    need = 2 if len(descriptors) >= 2 else 1
    return [c for c in cands if c["head_ok"] or c["matches"] >= need]

def _scrape(c):
    txt = card_text(c["btn"])
    src = c["name"] + " " + txt
    c["price"] = extract_price(txt)
    c["size_oz"] = parse_size(src)
    c["fluid"] = is_fluid(src) or any(k in c["name"].lower() for k in FLUID_HINTS)
    c["count"] = parse_count(src)
    c["is_bestseller"] = "best seller" in txt.lower() or "bestseller" in txt.lower()
    return c

def mark_bulk(c, produce):
    c["bulk"] = (
        c.get("bulk_word", False)
        or (produce and c.get("size_oz") is not None and c["size_oz"] >= 24)
        or (produce and c.get("count") is not None and c["count"] > 1)
    )
    return c

def _choose(options, req_oz, req_ct, produce=False):
    pool = options
    bestsellers = [c for c in pool if c.get("is_bestseller")]
    if bestsellers:
        pool = bestsellers
    if produce:
        singles = [c for c in pool if not c.get("bulk")]
        if singles:
            pool = singles
    gv = [c for c in pool if c["is_gv"]]
    pool = gv if (PREFER_BRAND and gv) else pool
    if req_oz is not None:
        sized = [c for c in pool if c["size_oz"] is not None]
        if sized:
            bd = min(abs(c["size_oz"] - req_oz) for c in sized)
            pool = [c for c in sized if abs(c["size_oz"] - req_oz) <= bd + 0.01]
    elif req_ct is not None:
        cnt = [c for c in pool if c["count"] is not None]
        if cnt:
            bd = min(abs(c["count"] - req_ct) for c in cnt)
            pool = [c for c in cnt if abs(c["count"] - req_ct) <= bd + 0.01]
    priced = [c for c in pool if c.get("price") is not None]
    return min(priced, key=lambda c: c["price"]) if priced else pool[0]

def opt_label(c, target_oz=None):
    if c.get("size_oz") is not None:
        sz = fmt_size(c["size_oz"], c.get("fluid", False))
    elif c.get("count"):
        sz = f"{c['count']} ct"
    else:
        sz = ""
    tag = f" {C.YELLOW}[bulk]{C.RESET}" if c.get("bulk") else ""
    p = f"{C.GREEN}${c['price']:.2f}{C.RESET}" if c.get("price") is not None else f"{C.GRAY}n/a{C.RESET}"
    hint = ""
    if target_oz and c.get("size_oz"):
        need = max(1, round(target_oz / c["size_oz"]))
        hint = f"  {C.GRAY}→ buy {need} for {fmt_size(target_oz)}{C.RESET}"
    nm = (c["name"][:38] + "…") if len(c["name"]) > 39 else c["name"]
    return f"{nm}  {C.DIM}{sz}{C.RESET} {p}{tag}{hint}"

def find_match(page, query, req_oz, req_ct):
    produce = query.lower().startswith("fresh ")
    relevant = _collect(page, query)
    used_q = query
    if not relevant:
        alt = broaden(query)
        if alt.lower() != query.lower():
            info(f"'{query}' not found — retrying as '{alt}'…")
            search_item(page, alt)
            relevant = _collect(page, alt)
            used_q = alt
    if not relevant:
        return None, [], used_q

    relevant.sort(key=lambda c: (not c["is_gv"], -c["type_score"]))
    options, seen = [], set()
    for c in relevant:
        if c["name"] in seen:
            continue
        seen.add(c["name"])
        mark_bulk(_scrape(c), produce)
        options.append(c)
        if len(options) >= 6:
            break
    if produce:
        options.sort(key=lambda c: c.get("bulk", False))

    chosen = _choose(options, req_oz, req_ct, produce)
    return chosen, options, used_q

def compute_status(chosen, it, query):
    if chosen is None:
        return "not-carried", ["Not carried at Walmart"]
    msgs, status = [], "added"
    name = chosen["name"]
    brand = query_brand_tokens(query)
    if brand and not any(b in name.lower() for b in brand):
        msgs.append(f"Brand swap — wanted '{' '.join(brand).title()}', got '{name}'")
        status = "substituted"
    if chosen.get("bulk"):
        msgs.append(f"Bulk pack — '{name}' is a bag/multi-pack, not a single")
        status = "bulk-pick"
    if chosen.get("size_oz") is not None and it["req_oz"] is not None:
        if abs(chosen["size_oz"] - it["req_oz"]) > SIZE_TOLERANCE * it["req_oz"]:
            want = fmt_size(it["req_oz"], it["req_fluid"])
            got = fmt_size(chosen["size_oz"], chosen["fluid"])
            msgs.append(f"Size differs — wanted {want}, closest is {got}")
            status = "size-mismatch"
    if not chosen["head_ok"]:
        msgs.append(f"Low confidence — '{name}' may be the wrong product")
        status = "low-confidence"
    return status, msgs

def commit(page, chosen, qty):
    name = chosen["name"]
    if chosen.get("size_oz") is not None:
        sz = fmt_size(chosen["size_oz"], chosen.get("fluid", False))
        total = f" {C.DIM}({fmt_size(chosen['size_oz'] * qty, chosen.get('fluid', False))} total){C.RESET}" if qty > 1 else ""
    elif chosen.get("count"):
        sz, total = f"{chosen['count']} ct", ""
    else:
        sz, total = "", ""
    price = chosen["price"]
    price_str = f"${price:.2f}" if price is not None else "n/a"

    btn = chosen["btn"]
    btn.scroll_into_view_if_needed(); time.sleep(0.4)
    try:
        btn.click(timeout=5000)
    except Exception:
        btn.click(force=True)
    time.sleep(2.0)
    handle_ripeness(page)

    added = 1
    if qty > 1:
        inc = page.locator(f'button[aria-label="Increment quantity of {name}"]')
        for _ in range(qty - 1):
            try:
                inc.first.wait_for(state="visible", timeout=4000)
                inc.first.click(timeout=4000); added += 1; time.sleep(1.0)
            except Exception:
                break
    in_cart = page.locator(f'button[aria-label="Increment quantity of {name}"]').count() > 0
    short = (name[:38] + "…") if len(name) > 39 else name
    if in_cart:
        ok(f"{C.BOLD}{short}{C.RESET}  {C.DIM}{sz}{C.RESET}  {C.GREEN}{price_str}{C.RESET} × {added}{total}")
    else:
        warn(f"{short} — added but not confirmed")
    return added, in_cart

def add_item(page, it):
    qty = it["qty"]
    search_item(page, it["query"])
    chosen, options, used_q = find_match(page, it["query"], it["req_oz"], it["req_ct"])

    if MANUAL_OVERRIDE:
        while True:
            status, msgs = compute_status(chosen, it, it["query"])
            if status == "added":
                break
            for m in msgs:
                warn(m)
            if options:
                target_oz = (it["req_oz"] * it["qty"]) if it["req_oz"] else None
                if target_oz:
                    info(f"Target total: {fmt_size(target_oz)} ({it['qty']} × {fmt_size(it['req_oz'])})")
                print(f"\n   {C.BOLD}Which one?{C.RESET}")
                for i, o in enumerate(options, 1):
                    mark = f"{C.GREEN}●{C.RESET}" if o is chosen else f"{C.GRAY}○{C.RESET}"
                    print(f"     {mark} {C.BOLD}{i}{C.RESET}  {opt_label(o, target_oz)}")
                print(f"   {C.GRAY}↳ Enter=accept · N=pick · N*Q=set qty · type=search · s=skip{C.RESET}")
            try:
                resp = input(f"   {C.CYAN}▸ {C.RESET}").strip()
            except EOFError:
                resp = ""
            if resp == "":
                break
            if resp.lower() == "s":
                info(f"Skipped '{used_q}'.")
                return {"name": f"(skipped) {it['query']}", "price": None, "qty": 0, "status": "skipped"}
            m = re.match(r"^(\d+)\s*(?:[x*]\s*|\s+)(\d+)$", resp)
            if m and 1 <= int(m.group(1)) <= len(options):
                chosen = options[int(m.group(1)) - 1]
                qty = int(m.group(2))
                break
            if resp.isdigit() and 1 <= int(resp) <= len(options):
                chosen = options[int(resp) - 1]
                break
            used_q = resp
            search_item(page, used_q)
            chosen, options, used_q = find_match(page, used_q, it["req_oz"], it["req_ct"])

    if chosen is None:
        err("Not carried at Walmart — skipped")
        return {"name": f"(not carried) {it['query']}", "price": None, "qty": 0, "status": "not-carried"}

    status, msgs = compute_status(chosen, it, it["query"])
    for m in msgs:
        warn(m)
    added, in_cart = commit(page, chosen, qty)
    return {"name": chosen["name"], "price": chosen["price"], "qty": added,
            "status": status if in_cart else "not-confirmed"}

def read_pasted_list():
    print(f"{C.GRAY}Paste your list (one item per line). Blank line to finish:{C.RESET}")
    lines = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if not line.strip():
            break
        lines.append(line.strip())
    return [parse_pasted_line(l) for l in lines]

banner("🛒  Walmart Grocery Agent")
print(f"\n{C.BOLD}How would you like to start?{C.RESET}")
print(f"   {C.BOLD}1{C.RESET}  Paste a shopping list")
print(f"   {C.BOLD}2{C.RESET}  Describe a recipe")
mode = input(f"   {C.CYAN}▸ {C.RESET}").strip()
items = read_pasted_list() if mode == "1" else get_grocery_list(input(f"   {C.CYAN}What are we cooking? ▸ {C.RESET}"))

banner("📋  Shopping List")
for it in items:
    sz = f" {C.DIM}[{it['size_str']}]{C.RESET}" if it.get("size_str") else ""
    print(f"   {C.GRAY}•{C.RESET} {it['query']} {C.DIM}× {it['qty']}{C.RESET}{sz}")

go = input(f"\n{C.BOLD}Add these to your cart?{C.RESET} {C.GRAY}[Enter = yes · q = quit]{C.RESET} {C.CYAN}▸ {C.RESET}").strip().lower()
if go == "q":
    raise SystemExit(f"{C.GRAY}Cancelled.{C.RESET}")

results = []
with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp(CDP_URL)
    page = browser.contexts[0].pages[0] if browser.contexts[0].pages else browser.contexts[0].new_page()
    if "instacart.com" not in page.url:
        err(f"Chrome is on {page.url}, not Instacart. Open the Walmart storefront, then re-run.")
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

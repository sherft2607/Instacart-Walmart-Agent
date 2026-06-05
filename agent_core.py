import os
import re
import json
import time
import hashlib
import sys
from pydantic import BaseModel, Field
from google import genai
from google.genai import errors
from google.genai.types import GenerateContentConfig
from playwright.sync_api import sync_playwright

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

api_key = os.environ.get("GEMINI_API_KEY")
env_file = ".env"

if (not api_key or api_key == "your-new-rotated-key-here") and os.path.exists(env_file):
    try:
        with open(env_file) as f:
            for line in f:
                if line.strip().startswith("GEMINI_API_KEY="):
                    api_key = line.split("=", 1)[1].strip().strip('"\'')
                    break
    except Exception:
        pass

if not api_key or api_key == "your-new-rotated-key-here":
    api_key = input("Enter your Gemini API Key: ").strip()
    if not api_key:
        raise SystemExit("Error: GEMINI_API_KEY is not set.")
    try:
        with open(env_file, "a") as f:
            f.write(f"\nGEMINI_API_KEY={api_key}\n")
        print(f"   {C.GREEN}✓{C.RESET} Saved API Key to {env_file} for future runs.")
    except Exception:
        pass

os.environ["GEMINI_API_KEY"] = api_key

client = genai.Client()

CDP_URL = "http://localhost:9223"
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

class GroceryItemSchema(BaseModel):
    query: str = Field(description="Main ingredient name (e.g. Great Value Shredded Cheddar Cheese, Fresh Broccoli)")
    qty: int = Field(description="Whole number quantity to purchase, defaults to 1")
    size: str = Field(description="Size if it matters (e.g. 16 oz, 12 count, blank if not important)")

class RecipeResponseSchema(BaseModel):
    items: list[GroceryItemSchema] = Field(description="List of ingredients to purchase")
    instructions: str = Field(description="Step-by-step cooking instructions for the recipe, or empty if not a recipe")

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
    
    # If the text starts with a number followed by a size unit, the qty is likely 1, not that number.
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
    q = re.sub(r'(?:\x1b)?\[[0-9;]*m', '', line)
    q = re.sub(r"^\s*\d+\s+", "", q)
    for w in WORD_NUM:
        q = re.sub(rf"^\s*{w}\b\s*", "", q, flags=re.I)
    q = re.sub(rf"\b\d*\s*{CONTAINERS}\s+of\b", "", q, flags=re.I)
    q = re.sub(rf"\b{CONTAINERS}\s+of\b", "", q, flags=re.I)
    q = re.sub(r"\b\d+(?:\.\d+)?\s*(?:fl\s*oz|oz|ounces?|lbs?|pounds?|pints?|quarts?|qts?|gallons?|gals?|grams?|g|ml|milliliters?|pt|pts)\b", "", q, flags=re.I)
    q = re.sub(r"\b\d+\s*(?:ct|count)\b", "", q, flags=re.I)
    return re.sub(r"\s{2,}", " ", q).strip(" ,-")

def parse_pasted_line(line):
    # Strip both raw and literal ANSI escape sequences (e.g. \x1b[1m or [1m)
    line = re.sub(r'(?:\x1b)?\[[0-9;]*m', '', line)
    size_str = ""
    m = re.search(r"\d+(?:\.\d+)?\s*(?:fl\s*oz|oz|lbs?|pounds?|ct|count|pints?|quarts?|qts?|gallons?|gals?|grams?|g|ml|milliliters?|pt|pts)\b", line.lower())
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
        config=GenerateContentConfig(
            system_instruction=system_instruction,
            temperature=0.1,
            response_mime_type="application/json",
            response_schema=RecipeResponseSchema
        ),
    )
    data = json.loads(resp.text)
    raw_items = data.get("items", [])
    instructions = data.get("instructions", "")

    if instructions:
        try:
            with open("recipe_instructions.md", "w") as f:
                f.write(f"# Recipe Instructions for: {user_input}\n\n{instructions}\n")
            ok("Saved cooking instructions to recipe_instructions.md")
        except Exception as e:
            warn(f"Failed to save recipe instructions: {e}")

    items = []
    for e in raw_items:
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
    return page.locator('input[aria-label="Search"]:visible, input[placeholder^="Search Walmart"]:visible').first

def handle_ripeness(page):
    try:
        dialog = page.locator('[role="dialog"], div[class*="modal"], div[class*="overlay"]').first
        if not (dialog.count() and dialog.is_visible()):
            return
        container = dialog
        
        # Check if this is actually a ripeness dialog
        dialog_text = container.inner_text().lower()
        is_ripeness = "ripe" in dialog_text or "ready to eat" in dialog_text
        
        if is_ripeness:
            # Find all elements containing the word "Ripe" (case-insensitive)
            opts = container.get_by_text("Ripe", exact=False)
            for i in range(opts.count()):
                opt = opts.nth(i)
                try:
                    if opt.is_visible():
                        text = opt.inner_text().lower()
                        # We want it to contain 'ripe', but NOT 'not' and NOT 'almost'
                        if "ripe" in text and "not" not in text and "almost" not in text:
                            opt.click(timeout=1500)
                            time.sleep(0.5)
                            break
                except Exception:
                    continue
            
            confirmed = False
            for label in ["Save", "Confirm", "Done", "Add", "Continue", "Choose"]:
                btns = container.locator(f'button:has-text("{label}")')
                for i in range(btns.count()):
                    btn = btns.nth(i)
                    if btn.is_visible():
                        btn.click(timeout=1500)
                        time.sleep(0.5)
                        confirmed = True
                        break
                if confirmed:
                    break
        else:
            # It's a promo / Family Cart modal! Dismiss it.
            info("Detected promo or family cart popup. Dismissing it...")
            dismissed = False
            for label in ["No thanks", "Dismiss", "Cancel", "Close", "Not now"]:
                btns = container.locator(f'button:has-text("{label}"), button:has-text("{label.lower()}")')
                for i in range(btns.count()):
                    btn = btns.nth(i)
                    if btn.is_visible():
                        btn.click(timeout=1500)
                        time.sleep(0.5)
                        dismissed = True
                        break
                if dismissed:
                    break
            
            # Escape fallback if standard buttons aren't present
            if not dismissed:
                page.keyboard.press("Escape")
                time.sleep(0.5)

        # Fallback safety: close modal if still visible to prevent blocking subsequent clicks
        if dialog.count() and dialog.is_visible():
            page.keyboard.press("Escape")
            time.sleep(0.5)
            for close_sel in ['button[aria-label="Close"]', 'button[aria-label="close"]', '[class*="close"]']:
                close_btn = dialog.locator(close_sel)
                if close_btn.count() and close_btn.first.is_visible():
                    close_btn.first.click(timeout=1000)
                    time.sleep(0.5)
                    break
    except Exception:
        pass
    except Exception:
        pass

def search_item(page, item):
    handle_ripeness(page)
    box = find_search_box(page)
    box.click()
    box.fill(item)
    box.press("Enter")
    
    # Wait for the search results page to start transitioning and clear old results
    time.sleep(1.2)
    
    # Wait for the new results to finish loading
    try:
        page.wait_for_load_state("networkidle", timeout=3000)
    except Exception:
        pass

def product_name_from_label(label):
    m = re.match(r"^Add\s+\d+\s+\S+\s+(.*)$", label)
    return m.group(1).strip() if m else label.replace("Add", "", 1).strip()

def card_text(button):
    try:
        return button.evaluate("""(btn) => {
            let el = btn;
            for (let i = 0; i < 10 && el; i++) {
                el = el.parentElement;
                if (el && el.innerText && el.innerText.includes('$')) {
                    return el.innerText;
                }
            }
            return "";
        }""")
    except Exception:
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

def is_produce(text):
    t = text.lower()
    if "fresh " in t:
        return True
    produce_keywords = {
        "banana", "garlic", "onion", "tomato", "potato", "apple", "orange", 
        "lemon", "lime", "lettuce", "spinach", "broccoli", "carrot", 
        "cucumber", "pepper", "avocado", "strawberry", "blueberry", "grape", 
        "peach", "pear", "plum", "melon", "watermelon", "cantaloupe", 
        "squash", "zucchini", "celery", "cabbage", "cauliflower", "ginger", 
        "cilantro", "parsley", "basil", "mint"
    }
    words = set(re.findall(r"[a-z]+", t))
    return bool(words & produce_keywords)

def _collect(page, query):
    q_lower = query.lower()
    produce = is_produce(q_lower)
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
            
            # Count extra non-generic, non-brand words in the product name
            name_words = [stem(w) for w in re.findall(r"[a-z]+", name.lower()) if len(w) > 2]
            extra_count = 0
            for w in name_words:
                if w not in BRAND_WORDS and w not in GENERIC_LEADERS and w not in GENERIC_HEADS:
                    if w not in descriptors:
                        extra_count += 1
                        
            # Penalize extra words to favor simpler/exact matches
            type_score = matches + (1 if head_ok else 0) - (0.5 * extra_count)
            cands.append({"btn": b, "name": name, "type_score": type_score,
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
    else:
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

def get_substitute_query(query):
    try:
        resp = generate_with_retry(
            model="gemini-2.5-flash",
            contents=f"Suggest a common storefront alternative ingredient stocked by Walmart for: '{query}'. Do NOT include the brand name (like Great Value). Output only the plain, generic name of the substitute.",
            config=GenerateContentConfig(temperature=0.2, max_output_tokens=25),
        )
        sub = resp.text.strip().strip('"\'-')
        # If the substitute is too short or is just a brand word, ignore it
        if len(sub) < 3 or sub.lower() in BRAND_WORDS:
            return None
        return sub
    except Exception:
        return None

def find_match(page, query, req_oz, req_ct):
    produce = is_produce(query)
    relevant = _collect(page, query)
    used_q = query
    
    # 1. Retry with broadened query (removes brand words like Great Value)
    if not relevant:
        alt = broaden(query)
        if alt.lower() != query.lower():
            info(f"'{query}' not found — retrying as '{alt}'…")
            search_item(page, alt)
            relevant = _collect(page, alt)
            used_q = alt
            
    # 2. Retry with last 2 words of the query (excl. quantities/units)
    if not relevant:
        exclude_words = {"pint", "pints", "pt", "pts", "quart", "quarts", "qt", "qts", "gallon", "gallons", "gal", "gals", "gram", "grams", "g", "ml", "oz", "lbs", "lb", "count", "ct", "pack", "pk"}
        words = [w for w in used_q.split() if w.lower() not in BRAND_WORDS and not w.isdigit() and w.lower() not in exclude_words]
        if len(words) > 1:
            alt2 = " ".join(words[-2:])
            if alt2.lower() != used_q.lower():
                info(f"'{used_q}' not found — retrying as '{alt2}'…")
                search_item(page, alt2)
                relevant = _collect(page, alt2)
                used_q = alt2
                
    # 3. Retry with last 1 word of the query (excl. quantities/units)
    if not relevant:
        exclude_words = {"pint", "pints", "pt", "pts", "quart", "quarts", "qt", "qts", "gallon", "gallons", "gal", "gals", "gram", "grams", "g", "ml", "oz", "lbs", "lb", "count", "ct", "pack", "pk"}
        words = [w for w in used_q.split() if w.lower() not in BRAND_WORDS and not w.isdigit() and w.lower() not in exclude_words]
        if len(words) > 0:
            alt3 = words[-1]
            if alt3.lower() != used_q.lower():
                info(f"'{used_q}' not found — retrying as '{alt3}'…")
                search_item(page, alt3)
                relevant = _collect(page, alt3)
                used_q = alt3

    # 4. Final fallback: AI suggested alternative (conserves daily API limits)
    if not relevant:
        alt_ai = get_substitute_query(query)
        if alt_ai and alt_ai.lower() != used_q.lower() and alt_ai.lower() != query.lower():
            warn(f"'{query}' not found — AI suggested substitute: '{alt_ai}'")
            search_item(page, alt_ai)
            relevant = _collect(page, alt_ai)
            used_q = alt_ai

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

def robust_click(btn):
    for attempt in range(3):
        try:
            btn.scroll_into_view_if_needed(timeout=2000)
            time.sleep(0.3)
            btn.click(timeout=3000)
            return True
        except Exception:
            try:
                btn.click(force=True, timeout=2000)
                return True
            except Exception:
                time.sleep(0.5)
    return False

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
    clicked = robust_click(btn)
    if not clicked:
        warn(f"Failed to click add button for '{name}' after retries")
    time.sleep(1.5)
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

USER_INPUT_CALLBACK = None

def get_user_input(options, used_q, it, chosen=None):
    if USER_INPUT_CALLBACK:
        return USER_INPUT_CALLBACK(options, used_q, it, chosen)
    return input("   ▸ ").strip()

def add_item(page, it):
    qty = it["qty"]
    explicit_qty = False
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
                import winsound
                winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
            except Exception:
                pass
            try:
                resp = get_user_input(options, used_q, it, chosen)
            except EOFError:
                resp = ""
            if resp == "":
                break
            if resp.lower() == "s":
                info(f"Skipped '{used_q}'.")
                return {"query": it['query'], "name": f"(skipped) {it['query']}", "price": None, "qty": 0, "status": "skipped"}
            m = re.match(r"^(\d+)\s*(?:[x*]\s*|\s+)(\d+)$", resp)
            if m and 1 <= int(m.group(1)) <= len(options):
                chosen = options[int(m.group(1)) - 1]
                qty = int(m.group(2))
                explicit_qty = True
                break
            if resp.isdigit() and 1 <= int(resp) <= len(options):
                chosen = options[int(resp) - 1]
                break
            used_q = resp
            search_item(page, used_q)
            chosen, options, used_q = find_match(page, used_q, it["req_oz"], it["req_ct"])

    if chosen is not None and not explicit_qty:
        if it.get("req_oz") and chosen.get("size_oz"):
            target_oz = it["req_oz"] * it["qty"]
            qty = max(1, round(target_oz / chosen["size_oz"]))

    if chosen is None:
        err("Not carried at Walmart — skipped")
        return {"query": it['query'], "name": f"(not carried) {it['query']}", "price": None, "qty": 0, "status": "not-carried"}

    status, msgs = compute_status(chosen, it, it["query"])
    for m in msgs:
        warn(m)
    added, in_cart = commit(page, chosen, qty)
    return {"query": it['query'], "name": chosen["name"], "price": chosen["price"], "qty": added,
            "status": status if in_cart else "not-confirmed"}

def empty_cart(page):
    try:
        print("Emptying cart...")
        # Open cart drawer
        cart_btn = page.locator('a[href="/store/walmart/cart"], button[aria-label*="cart" i], a[aria-label*="cart" i]').first
        if cart_btn.count() > 0:
            cart_btn.click(force=True)
            time.sleep(2)
            
        # Spam remove and decrease quantity buttons until they are all gone
        for _ in range(50):
            dec_btn = page.locator('button[aria-label^="Decrease quantity"], button[aria-label^="Remove item"], button[aria-label^="Remove"]').first
            if dec_btn.count() > 0 and dec_btn.is_visible():
                try:
                    dec_btn.click(force=True)
                    time.sleep(0.5)
                except:
                    pass
            else:
                # Try finding text="Remove" inside the drawer
                remove_text = page.locator('button:has-text("Remove")').first
                if remove_text.count() > 0 and remove_text.is_visible():
                    try:
                        remove_text.click(force=True)
                        time.sleep(0.5)
                    except:
                        pass
                else:
                    break
                    
        # Close cart
        close_btn = page.locator('button[aria-label="Close"], button[aria-label="Close modal"]').first
        if close_btn.count() > 0 and close_btn.is_visible():
            close_btn.click(force=True)
        print("Cart emptied!")
    except Exception as e:
        print(f"empty_cart failed: {e}")

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

def filter_pantry(items):
    pantry_file = "pantry.txt"
    if not os.path.exists(pantry_file):
        try:
            with open(pantry_file, "w") as f:
                f.write("# Add pantry items here (one per line). Lines starting with # are ignored.\n")
                f.write("salt\npepper\nwater\nolive oil\nvegetable oil\nsugar\nflour\nbutter\n")
        except Exception:
            pass
        return items
    try:
        with open(pantry_file) as f:
            pantry_items = [line.strip().lower() for line in f if line.strip() and not line.strip().startswith("#")]
        filtered = []
        for it in items:
            q = it["query"].lower()
            if any(p in q for p in pantry_items):
                info(f"Skipped pantry item: '{it['query']}'")
            else:
                filtered.append(it)
        return filtered
    except Exception as e:
        warn(f"Failed to read pantry list: {e}")
        return items

def propose_item(page, it):
    qty = it["qty"]
    search_item(page, it["query"])
    chosen, options, used_q = find_match(page, it["query"], it["req_oz"], it["req_ct"])
    
    if chosen is None:
        return {"query": it["query"], "name": f"(not carried) {it['query']}", "price": None, "qty": 0, "status": "not-carried", "options": []}

    if it.get("req_oz") and chosen.get("size_oz"):
        target_oz = it["req_oz"] * it["qty"]
        qty = max(1, round(target_oz / chosen["size_oz"]))

    status, msgs = compute_status(chosen, it, it["query"])
    
    runner_ups = []
    for o in options[:4]:
        need = it["qty"]
        if it.get("req_oz") and o.get("size_oz"):
            need = max(1, round((it["req_oz"] * it["qty"]) / o["size_oz"]))
        runner_ups.append({
            "name": o["name"],
            "price": o["price"],
            "qty": need,
            "is_gv": o.get("is_gv", False),
            "size_str": fmt_size(o["size_oz"], o.get("fluid", False)) if o.get("size_oz") else ""
        })

    return {
        "query": it["query"],
        "name": chosen["name"], 
        "price": chosen["price"], 
        "qty": qty,
        "status": status,
        "options": runner_ups
    }

def commit_exact_item(page, exact_name, qty):
    info(f"Committing item: {exact_name} (qty {qty})")
    search_item(page, exact_name)
    
    # Wait up to 6 seconds for a button that matches our exact name
    for attempt in range(12):
        buttons = page.locator('button[aria-label^="Add"]')
        for i in range(min(buttons.count(), 15)):
            b = buttons.nth(i)
            try:
                if not b.is_visible(): continue
                raw = b.get_attribute("aria-label") or ""
                name = product_name_from_label(raw)
                if attempt == 0 and i == 0:
                    info(f"  First button name: {name}")
                if name.lower() == exact_name.lower() or exact_name.lower() in name.lower() or name.lower() in exact_name.lower():
                    chosen = {"name": name, "btn": b, "price": None}
                    added, in_cart = commit(page, chosen, qty)
                    ok(f"Added {name} to cart")
                    return {"name": name, "qty": added, "price": None, "status": "added" if in_cart else "not-confirmed"}
            except Exception as e:
                err(f"  Err checking button: {e}")
        time.sleep(0.5)
        
    err(f"Failed to find button matching: {exact_name}")
    return {"name": exact_name, "qty": 0, "price": None, "status": "error"}

# 🛒 Instacart Walmart Grocery Agent

A premium, local web dashboard and automation agent that parses recipes using Gemini and automatically searches for and adds items to your Instacart Walmart storefront cart using Playwright.

![Interface Preview](https://img.shields.io/badge/Interface-Glassmorphic%20Web%20%2F%20CLI-emerald)
![AI Model](https://img.shields.io/badge/AI%20Model-Gemini%202.5%20Flash-blue)
![Automation](https://img.shields.io/badge/Automation-Playwright-orange)

---

## ✨ Features

- **Double-Click Startup**: Automatically checks for Chrome debug port conflicts (D5 Render, etc.), binds to port `9223` if empty, starts the local server, and launches Chrome automatically.
- **Vibrant Web Dashboard**: A high-end glassmorphic web interface containing recipe text areas, parsed list review grids, and live progress terminals.
- **Gemini Structured JSON Outputs**: Leverages Google GenAI JSON schema enforcement to parse recipes into structured grocery items with quantity and sizing.
- **Smart Product Selection & Penalty Scoring**: Scores products using custom word-overlap matching. Unrelated flavored items (like protein shakes or bread) are penalized, prioritizing plain/exact produce matches (like raw bananas).
- **Interactive Choice Overlays**: If the agent is unsure about a brand swap or sized pack, it pauses and opens an inline selection card modal on your webpage, resuming once you click or type a search query.
- **Advanced Dialog Handling**: Automatically detects and answers Instacart ripeness popups (choosing "Ripe"), and immediately dismisses irrelevant promos (like "Family Cart" or subscription screens).
- **Session Persistence**: Saves your browser profile state locally, keeping you logged in to Instacart across runs.
- **Pantry Exclusion Filter**: Automatically filters out staples you already own (e.g. salt, pepper, olive oil) via a custom `pantry.txt`.

---

## 🛠️ Prerequisites

- **Python 3.8+**
- **Google Chrome** (installed in standard location)
- **Playwright Chromium binaries**

---

## 🚀 Setup & Installation

1. **Clone the repository**:
   ```bash
   git clone <your-repo-url>
   cd instacart
   ```

2. **Install dependencies**:
   ```bash
   pip install google-genai playwright flask pydantic
   playwright install chromium
   ```

3. **Get your Gemini API Key**:
   * Obtain a free key from the [Google AI Studio](https://aistudio.google.com/).
   * Create a `.env` file in the root folder (or the script will prompt you on first run to auto-create it):
     ```env
     GEMINI_API_KEY=your_gemini_api_key_here
     ```

---

## 💻 Usage

Double-click `run_agent.bat` inside the project folder:

1. **Select mode**:
   - `1` for the **Web App Dashboard** (recommended).
   - `2` for the **Terminal CLI Mode**.
2. If using the Web App, Chrome will open directly to `http://127.0.0.1:5000`.
3. Paste a shopping list or describe a recipe, review the items, click **Send to Cart**, and watch the automation run!

---

## 📂 Project Structure

```text
instacart/
├── run_agent.bat        # Double-click launcher (handles ports & server startup)
├── app.py               # Flask web server (routes, SSE logs streaming, dynamic browser launch)
├── grocery_agent.py     # Interactive Terminal CLI interface wrapper
├── agent_core.py        # Central automation library (Playwright actions & logic)
├── pantry.txt           # Staples to filter out (auto-created)
├── order_history.json   # Logged order history (auto-created)
├── .gitignore           # Prevents uploading logins, caches, and API keys
└── README.md            # You are here!
```

---

## 🔒 Security Notice

The `.gitignore` is pre-configured to **never** upload:
- `.env` (API Keys)
- `playwright_user_data/` (Your local browser login cookies and session)
- `grocery_cache.json` (Local caching)
- `order_history.json` (Personal purchasing logs)

Ensure these remain untracked before publishing your repository.

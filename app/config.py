"""Settings, read once from environment variables (or a local .env file)."""
import os

from dotenv import load_dotenv

load_dotenv()


class Settings:
    # --- LLM: Groq (free tier, no card needed) ---
    groq_api_key: str = os.getenv("GROQ_API_KEY", "")
    # Defaults checked against console.groq.com/docs (Nov 2026). llama-3.3-70b-versatile and
    # llama-4-scout were retired by Groq, so they return model_not_found.
    groq_model: str = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
    # Used only when a screenshot is uploaded (needs a model that accepts images).
    groq_vision_model: str = os.getenv("GROQ_VISION_MODEL", "qwen/qwen3.8-27b")

    # --- Web search for the decision-maker lookup: Tavily (free tier) ---
    # Leave TAVILY_API_KEY blank to use only the company's own website.
    tavily_api_key: str = os.getenv("TAVILY_API_KEY", "")
    enable_web_search: bool = os.getenv("ENABLE_WEB_SEARCH", "true").lower() == "true"
    max_web_searches: int = int(os.getenv("MAX_WEB_SEARCHES", "3"))

    # --- Apps Script web app (recommended: no Google Cloud project needed) ---
    apps_script_url: str = os.getenv("APPS_SCRIPT_URL", "")
    apps_script_token: str = os.getenv("APPS_SCRIPT_TOKEN", "")

    # --- Google Sheets (leave blank to fall back to a local CSV file) ---
    google_sheet_id: str = os.getenv("GOOGLE_SHEET_ID", "")
    google_credentials_file: str = os.getenv("GOOGLE_CREDENTIALS_FILE", "service_account.json")
    google_credentials_json: str = os.getenv("GOOGLE_CREDENTIALS_JSON", "")  # for cloud deploys
    google_worksheet_name: str = os.getenv("GOOGLE_WORKSHEET_NAME", "Leads")
    csv_fallback_path: str = os.getenv("CSV_FALLBACK_PATH", "leads.csv")

    # --- Telegram ---
    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_webhook_secret: str = os.getenv("TELEGRAM_WEBHOOK_SECRET", "change-me")

    # --- Sender identity used in the drafts ---
    sender_name: str = os.getenv("SENDER_NAME", "Team GoPratle")


settings = Settings()
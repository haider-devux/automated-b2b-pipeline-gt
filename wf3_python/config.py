"""
WF-3 configuration: database + LLM settings.
👉 Before running, set DB_PASSWORD below (or as an environment variable). See WF3_GUIDE.md.
Secrets (PAGESPEED_API_KEY, DB_PASSWORD, GMAIL_*, ...) can also live in a project-root .env file.
"""
import os
from pathlib import Path

# Load secrets from the project-root .env (if python-dotenv is installed). override=True so .env is
# the single source of truth even if an older value lingers in the OS environment (e.g. a past setx).
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)
except ImportError:
    pass  # dotenv is optional — plain env vars still work without it

# ---- Postgres (your existing native DB — no Docker needed) ----
DB = {
    "host": os.getenv("DB_HOST", "localhost"),
    "port": int(os.getenv("DB_PORT", "5432")),
    "dbname": os.getenv("DB_NAME", "granjur_pipeline"),
    "user": os.getenv("DB_USER", "postgres"),
    # 🔑 Set DB_PASSWORD in the project-root .env (never hardcode it — .env is gitignored).
    "password": os.getenv("DB_PASSWORD", ""),
}

# ---- Ollama (local LLM — used ONLY for writing the pitch + translating it) ----
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434/api/generate")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:3b")
OLLAMA_TEMPERATURE = 0.2  # low = less invention (factual consistency matters more than flair for cold pitch)
OLLAMA_TIMEOUT = 300  # seconds — local CPU inference is slow, give it room
PITCH_MAX_ATTEMPTS = 3  # small models sometimes drop the body; retry before falling back

# ---- Pitch quality gates (a professional-length, grounded cold pitch) ----
PITCH_MIN_WORDS = 70        # too-short reads as unprofessional -> retry if the model returns a stub
PITCH_MAX_WORDS = 140       # ~3 short paragraphs (research / offer / CTA). Enforced in prompt + code
PITCH_GROUNDING_MIN = 0.6   # min share of prospect-claims backed by a verified fact to accept without retry

# ---- Mappings ----
LANG_MAP = {"GCC": "ar", "CN": "zh"}   # region -> pitch language ("en" is the default)
SEGMENT_DB = {                          # segment letter -> DB enum value
    "A": "A_LEGACY_BRICK",
    "B": "B_FUNDED_STARTUP",
    "C": "C_LOWTECH_ECOM",
}

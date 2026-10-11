"""Все настройки бота из окружения / .env — в одном месте.

Импортируй этот модуль раньше остальных: он загружает `.env` рядом с ботом.
Уже заданные переменные окружения не перетираются.
"""
import os
from urllib.parse import urlsplit

from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))


def env_any(*names: str) -> str | None:
    """Первое непустое значение среди нескольких имён переменных окружения."""
    for name in names:
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip()
    return None


def data_dir() -> str:
    """Каталог состояния (читается при каждом вызове — тесты подменяют DATA_DIR)."""
    return os.environ.get("DATA_DIR") or BASE_DIR


# ── Discord ───────────────────────────────────────────────────────────────────
DISCORD_TOKEN = env_any("DISCORD_BOT_TOKEN")
# ID серверов для мгновенной регистрации команд; пусто → глобальная (до ~1 ч).
GUILD_IDS = [int(g) for g in (os.environ.get("DISCORD_GUILD_IDS") or "").replace(";", ",").split(",")
             if g.strip().isdigit()]
_cap = env_any("MAX_UPLOAD_MB")
UPLOAD_CAP_MB = int(_cap) if _cap and _cap.isdigit() else None
HEALTH_PORT = int(os.environ["PORT"]) if (os.environ.get("PORT") or "").isdigit() else None

# ── Booru ─────────────────────────────────────────────────────────────────────
GELBOORU_API_KEY = env_any("GELBOORU_API_KEY")
GELBOORU_USER_ID = env_any("GELBOORU_USER_ID")
PROXY = env_any("GELBOORU_PROXY", "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")

_timeout = float(os.environ.get("SEARCH_TIMEOUT_SECONDS", "20"))
if not 0 < _timeout <= 600:
    raise ValueError("SEARCH_TIMEOUT_SECONDS должен быть положительным числом до 600")
SEARCH_TIMEOUT = min(_timeout, 30.0)  # старые env=120 не должны вернуть долгие ожидания

# ── Telegram ──────────────────────────────────────────────────────────────────
TG_API_ID = env_any("TG_API_ID", "API_ID", "TELEGRAM_API_ID", "TG_ID")
TG_API_HASH = env_any("TG_API_HASH", "API_HASH", "TELEGRAM_API_HASH", "TG_HASH")
TG_SESSION_STRING = env_any("TG_SESSION_STRING", "SESSION_STRING", "TG_SESSION",
                            "STRING_SESSION", "TG_STRING_SESSION")


def masked_proxy() -> str | None:
    """Прокси для логов — без логина/пароля."""
    if not PROXY:
        return None
    parts = urlsplit(PROXY)
    auth = "***@" if parts.username else ""
    return f"{parts.scheme}://{auth}{parts.hostname or '?'}:{parts.port or ''}"

# Gelbooru Discord Bot (v2.5)

Discord-бот на `nextcord`, который ищет арты по тегам на booru-сайтах и в
Telegram-каналах и присылает картинки / GIF / видео.

## Команды
- `/gelbooru <тег> [тег2] [тег3] [тег4]` — случайный арт по 1–4 тегам с **Gelbooru**.
  Приоритет отдаётся твоему запросу: ищешь часть тела (breasts/ass/pussy/feet) —
  наверх идёт арт, где она и есть сюжет, а не фоновый тег.
- `/konachan <тег> [тег2] [тег3] [тег4]` — то же с **Konachan** (аниме-арт).
- `/safebooru <тег> [тег2] [тег3] [тег4]` — **Safebooru**, safe-контент;
  работает в любом канале.
- `/tg [канал]` — топовый по реакциям арт из **Telegram**-канала (реклама
  отсеивается). Канал можно выбрать из списка (автодополнение) или оставить
  пустым — возьмётся случайный.
- `/tags` — статус популярных тегов (есть ли по ним арты).
- `/tagcheck <тег>` — проверить, активен ли тег на Gelbooru.
- `/help` — справка.

NSFW-команды (`/gelbooru`, `/konachan`, `/tg`) работают только в NSFW-каналах
или в личных сообщениях. `/safebooru` — в любом канале.

## Запуск

```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt

export DISCORD_BOT_TOKEN="ваш_токен_бота"
# опционально (рекомендуется — снимает лимиты Gelbooru):
export GELBOORU_API_KEY="ваш_api_key"
export GELBOORU_USER_ID="ваш_user_id"

python bot_gelbooru.py
```

На Windows (PowerShell):

```powershell
$env:DISCORD_BOT_TOKEN="ваш_токен_бота"
python bot_gelbooru.py
```

## Переменные окружения
- `DISCORD_BOT_TOKEN` — токен бота (обязательно).
- `GELBOORU_API_KEY` / `GELBOORU_USER_ID` — ключи Gelbooru (рекомендуется).
- `DISCORD_GUILD_IDS` — ID серверов через запятую для **мгновенной** регистрации
  слэш-команд (guild-режим). Пусто → глобальная регистрация (обновляется до ~1 ч).
  Бот сам сносит «лишний» скоуп при старте, чтобы команды не двоились в списке.
- `GELBOORU_PROXY` / `HTTPS_PROXY` — прокси к Gelbooru, если сайт заблокирован.
- `TG_API_ID`, `TG_API_HASH`, `TG_SESSION_STRING` — для команды `/tg`
  (Telegram-userbot через Telethon). Без них `/tg` просто отключается.

## Важно про безопасность
Токен бота и ключ Gelbooru читаются из переменных окружения, а не зашиты в код.
Никогда не публикуйте токен: если он попал в чат/репозиторий, сбросьте его
в Discord Developer Portal → Bot → Reset Token.

## Тесты
```bash
python test_bot.py
```

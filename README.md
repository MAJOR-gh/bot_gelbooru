# Gelbooru Discord Bot (v1.5)

Discord-бот на `nextcord`, который ищет по тегам на Gelbooru и присылает картинки / GIF / видео.

## Команды
- `/gelbooru <тег> [тег2] [тег3] [тег4]` — случайный результат по 1–4 тегам.
- `/safebooru <тег> [тег2] [тег3] [тег4]` — то же, но safe-контент; работает в любом канале.
- `/tags` — статус популярных тегов (есть ли по ним картинки).
- `/tagcheck <тег>` — проверить, активен ли тег.
- `/help` — справка.

Команды с контентом работают только в NSFW-каналах или в личных сообщениях.

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

## Важно про безопасность
Токен бота и ключ Gelbooru больше **не зашиты в код** — они читаются из переменных
окружения. Никогда не публикуйте токен: если он попал в чат/репозиторий, сбросьте его
в Discord Developer Portal → Bot → Reset Token.

## Тесты
```bash
python test_bot.py
```

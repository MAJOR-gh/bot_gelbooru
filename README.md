# Gelbooru Discord Bot (v3.0)

Discord-бот на `nextcord`: ищет арты по тегам на booru-сайтах и в
Telegram-каналах и присылает картинки / GIF / видео. Показывает лучший по
лайкам арт, который по этому запросу ещё не показывал.

## Команды
- `/gelbooru <тег> [тег2] [тег3] [тег4]` — арт по 1–4 тегам с **Gelbooru**.
  Ищешь часть тела (breasts/ass/pussy/feet) — наверх идёт арт, где она и есть
  сюжет, а не фоновый тег. `-тег` исключает тег.
- `/konachan <тег> …` — то же с **Konachan** (аниме-арт).
- `/safebooru <тег> …` — **Safebooru**, только safe-контент; работает в любом канале.
- `/tg [канал]` — топовый по реакциям пост из **Telegram**-канала (реклама
  отсеивается), весь альбом целиком.
- `/tags` — популярные теги и сколько по ним артов.
- `/tagcheck <тег>` — есть ли тег на Gelbooru + похожие теги при опечатке.
- `/help` — справка.

NSFW-команды (`/gelbooru`, `/konachan`, `/tg`, `/tagcheck`) работают только в
NSFW-каналах или в личке.

📘 **[Гайд разработчика](docs/GUIDE.md)** — как всё устроено и как менять.
❓ **[FAQ для пользователей](docs/FAQ.md)** — как пользоваться ботом.

## Структура
| Файл | Что внутри |
|---|---|
| `bot_gelbooru.py` | запуск, слэш-команды, отправка в Discord |
| `content_filter.py` | **все блэклисты** и правила (HARD-блок, нагота, фокус части тела) — править теги тут |
| `booru.py` | Gelbooru / Konachan / Safebooru: запросы, листание страниц, скачивание |
| `selection.py` | анти-повтор (`recent_shown.json`) и порядок выбора |
| `tg_source.py` | Telegram-каналы (Telethon) |
| `tg_channels.json` | список каналов для `/tg` |
| `assets/` | картинки для приколов |

## Переменные окружения
Кладутся в файл `.env` рядом с ботом (шаблон — `.env.example`) или в
переменные окружения хостинга.

- `DISCORD_BOT_TOKEN` — токен бота (**обязательно**).
- `GELBOORU_API_KEY` / `GELBOORU_USER_ID` — **обязательно для /gelbooru**: без
  ключа Gelbooru отвечает 401. Берутся на gelbooru.com → My Account → Options.
- `DISCORD_GUILD_IDS` — ID серверов через запятую для **мгновенной** регистрации
  команд. Пусто → глобальная регистрация (до ~1 ч).
- `TG_API_ID`, `TG_API_HASH`, `TG_SESSION_STRING` — для `/tg`. Строку сессии
  выдаёт `python tg_export_session.py`. Без них `/tg` отключена.
- Необязательно: `GELBOORU_PROXY` (если сайты заблокированы), `MAX_UPLOAD_MB`
  (потолок размера файла), `DATA_DIR` (куда писать память показанного),
  `PORT` (если хостинг его задаёт — поднимается health-эндпоинт `/health`).

## Запуск локально
```bash
python -m venv venv
venv\Scripts\activate            # Linux/macOS: source venv/bin/activate
pip install -r requirements.txt
copy .env.example .env           # и впиши токен/ключи
python bot_gelbooru.py
```

## Хостинг 24/7
Сейчас бот живёт на [Wispbyte](https://wispbyte.com/client) (бесплатно).
Обновление: push в `main` → **Restart** в панели — при старте сервер сам
скачивает свежий код и ставит зависимости. Подробно — в
[гайде, раздел 12](docs/GUIDE.md#12-хостинг-wispbyte).

Подойдёт и любой другой хостинг с Python 3.11+ и постоянным процессом:
склонировать репо, `pip install -r requirements.txt`, положить `.env`,
запускать `python bot_gelbooru.py`.

## Безопасность
Токены и ключи — только в `.env`/переменных хостинга, в git они не попадают.
Если токен утёк — Discord Developer Portal → Bot → Reset Token.

## Тесты
```bash
python test_bot.py
```

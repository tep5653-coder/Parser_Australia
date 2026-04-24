# Football NSW Lineups Monitor

Production-ready async Python utility for monitoring Football NSW fixtures and lineup publication.

## Что делает скрипт

1. Открывает страницу fixtures и находит ближайшие предстоящие матчи.
2. За `--prestart-minutes` до начала матча начинает опрос карточки матча каждую `--poll-interval-seconds`.
3. Ждет публикацию стартовых составов (11+11).
4. Ищет предыдущий сыгранный матч.
5. Считает изменения в стартовом составе каждой команды.
6. Если у любой команды 3+ изменения — отправляет Telegram alert.
7. Если к старту составов нет, мониторит еще `--post-start-grace-minutes` и завершает.

---

## Установка (macOS / Linux)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip3 install -r requirements.txt
python3 -m playwright install chromium
```

---

## Запуск в dry-run (без Telegram)

`--dry-run` не требует `TELEGRAM_BOT_TOKEN` и `TELEGRAM_CHAT_ID`.

```bash
python3 monitor.py --dry-run --limit 2 --log-level DEBUG
```

Рекомендуемый диагностический запуск (видимый браузер):

```bash
python3 monitor.py --dry-run --headless false --browser-channel chrome --log-level DEBUG
```

---

## Запуск с Telegram

Через ENV:

```bash
export TELEGRAM_BOT_TOKEN="123:abc"
export TELEGRAM_CHAT_ID="-100000000"
python3 monitor.py --headless false
```

Или через CLI:

```bash
python3 monitor.py \
  --telegram-token "123:abc" \
  --telegram-chat-id "-100000000" \
  --headless false \
  --browser-channel chrome
```

---

## Cloudflare: почему нужен headed-режим

Сайт может отдавать challenge-страницу Cloudflare (например, `Attention Required! | Cloudflare`) в headless-режиме.

Скрипт явно проверяет title/body и в логах пишет понятную причину (`Cloudflare challenge detected...`).

Чтобы пройти challenge вручную:

1. Запустите с `--headless false` (по умолчанию уже false).
2. В открытом браузере пройдите проверку Cloudflare.
3. После этого скрипт продолжит парсинг.

---

## Основные параметры CLI

- `--fixtures-url`
- `--timezone-offset-minutes` (по умолчанию `180`)
- `--limit` (по умолчанию `3`)
- `--headless` / `--no-headless` (по умолчанию headed)
- `--dry-run`
- `--poll-interval-seconds` (по умолчанию `60`)
- `--prestart-minutes` (по умолчанию `60`)
- `--post-start-grace-minutes` (по умолчанию `5`)
- `--browser-channel` (например `chrome`, можно задать через `PLAYWRIGHT_BROWSER_CHANNEL`)
- `--hydration-wait-seconds` (по умолчанию `8.0`)
- `--telegram-token`
- `--telegram-chat-id`
- `--log-level`

---

## Troubleshooting

### 1) `Cloudflare challenge detected`
- Запустите в headed: `python3 monitor.py --dry-run --headless false`.
- Пройдите challenge вручную в открывшемся окне.

### 2) `No upcoming fixtures parsed`
- Возможна смена верстки сайта.
- Включите `--log-level DEBUG` и проверьте, какие кандидаты матчей извлекаются.
- Для Football NSW на macOS часто помогает запуск через системный Chrome:
  `python3 monitor.py --dry-run --headless false --browser-channel chrome --log-level DEBUG`.

### 3) `Telegram token/chat_id missing`
- Либо задайте `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`, либо используйте `--dry-run`.

### 4) Playwright browser not installed

```bash
python3 -m playwright install chromium
```

### 5) `fatal: not a git repository (or any of the parent directories): .git`
- Это означает, что проект открыт из ZIP-архива (`Download ZIP`), а не из `git clone`.
- В ZIP нет папки `.git`, поэтому `git fetch / merge / push` не работают.
- Решение: заново клонировать репозиторий и работать уже в git-копии.

```bash
cd ~/Bet
git clone https://github.com/<YOUR_GITHUB_USERNAME>/Parser_Australia.git
cd <REPO_DIR>
git checkout <YOUR_BRANCH>
```

### 6) `This branch has conflicts that must be resolved`
- Подтяните актуальный `main` в вашу ветку, исправьте маркеры конфликта и отправьте обновления в PR:

```bash
git fetch origin
git merge origin/main
git status
```

- Откройте конфликтные файлы, удалите блоки `<<<<<<<`, `=======`, `>>>>>>>`, оставив итоговый код, затем:

```bash
git add monitor.py
git add test_monitor.py
git commit -m "Resolve merge conflicts in monitor.py and test_monitor.py"
git push
```

### 7) Пошагово (одна команда = один шаг) для конфликтов PR
Если вы сейчас в `~/Bet` и хотите пройти процесс без лишних действий:

```bash
git clone https://github.com/<YOUR_GITHUB_USERNAME>/Parser_Australia.git
```

Для текущего репозитория (владелец `tep5653-coder`) готовая команда:

```bash
git clone https://github.com/tep5653-coder/Parser_Australia.git
```

```bash
cd <REPO_DIR>
```

```bash
git checkout codex/implement-football-match-monitoring-script
```

```bash
git fetch origin
```

```bash
git merge origin/main
```

```bash
git status
```

После этого вручную исправьте `<<<<<<< ======= >>>>>>>` в конфликтных файлах.

```bash
git add monitor.py
```

```bash
git add test_monitor.py
```

```bash
git commit -m "Resolve merge conflicts in monitor.py and test_monitor.py"
```

```bash
git push
```

### 8) Нужно ли нажимать **Update branch / Обновить ветку** в GitHub?
- **Если есть конфликты** (`This branch has conflicts that must be resolved`) — кнопка обычно не решит проблему автоматически. Делайте локальный merge и ручное исправление конфликтов по шагам выше.
- **Если конфликтов нет** — кнопку нажимать можно, но это необязательно: тот же результат дает локально `git fetch` + `git merge origin/main` + `git push`.
- Для этого проекта безопасный и предсказуемый путь: сначала локально синхронизировать ветку, затем исправить конфликты и только после этого пушить изменения в PR.

---

## Проверка

```bash
python3 -m compileall monitor.py
pytest -q
```

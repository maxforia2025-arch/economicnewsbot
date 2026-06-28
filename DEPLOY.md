# 🚀 Деплой в облако (GitHub Actions) — постинг 24/7

Бот будет постить каждые 2 часа в облаке GitHub, **даже когда Mac выключен или спит**.
Токен НЕ хранится в репозитории — он идёт в секреты GitHub.

Всё уже подготовлено и закоммичено локально. Осталось 3 шага (≈3 минуты).

---

## Шаг 1. Создать пустой репозиторий на GitHub

1. Открой https://github.com/new
2. Имя: `economicnewsbot` (или любое).
3. Выбери **Private** (приватный — надёжнее).
4. **НЕ** добавляй README/.gitignore (они уже есть).
5. Нажми **Create repository**.

## Шаг 2. Запушить код (в Терминале)

Скопируй и выполни (подставь свой логин, если он другой):

```bash
cd "/Users/maksim/Desktop/YOUTUBE 1/economic_news_bot"
git remote add origin https://github.com/maxforia2025-arch/economicnewsbot.git
git push -u origin main
```

Если попросит логин/пароль — пароль это твой **Personal Access Token** GitHub
(как при настройке flyever-bot). Один раз — потом сохранится в связке ключей.

## Шаг 3. Добавить секрет с токеном бота

1. В репозитории: **Settings** → **Secrets and variables** → **Actions**.
2. **New repository secret**:
   - Name: `BOT_TOKEN`
   - Secret: токен бота от @BotFather (тот, что лежит в локальном secret.json)
   - **Add secret**.
3. (Необязательно) ещё один секрет:
   - Name: `CHANNEL_ID`  ·  Secret: `@economicrussialetsgo`
   (канал уже прописан в config.json, так что это можно пропустить.)

---

## Проверка

1. Вкладка **Actions** → если просит — нажми **I understand my workflows, enable them**.
2. Слева выбери workflow **economicnewsrussiabot** → **Run workflow** → **Run**.
3. Через ~1 минуту в канале @economicrussialetsgo появятся свежие посты,
   а в логе запуска будет строка `Опубликовано`.

После этого бот работает сам: каждые 2 часа, без участия Mac. Готово.

---

## Как поменять частоту / порог

- Частота: в `.github/workflows/post.yml` строка `cron: "0 */2 * * *"`
  (`*/2` = каждые 2 часа; `*/1` — каждый час).
- Порог цитируемости / источники / ключевые слова: `config.json`.
  После правок: `git commit -am "tune" && git push` — облако подхватит само.

# BEK264_rating_bot

Telegram-бот с журналом взаимных оценок группы. Логика оценок и меню находится в `bot.py`, обмен с Telegram в `run.py`, запуск и выбор серверного журнала в `main.py`.

## Один раз подключить действующий сервер Altare

1. Открой команду в [Altare](https://console.altare.gg), затем **Settings → API tokens → Create token → Custom**. Включи **servers:files** и **servers:power**. В **Only some apps** выбери один действующий сервер с ботом и скопируй ключ.
2. Открой [секреты GitHub Actions](https://github.com/spyCHka/BEK264_rating_bot/settings/secrets/actions), нажми **New repository secret**, укажи имя `ALTARE_TOKEN` и вставь ключ Altare.
3. На странице [Обновить бота](https://github.com/spyCHka/BEK264_rating_bot/actions/workflows/deploy.yml) выбери **Run workflow → main → Run workflow**. После успешного запуска отправь боту `/table` в Telegram, чтобы проверить журнал.

Текущая команда запуска — `/usr/local/bin/python /home/container/main.py`. Для первого обновления нужен уже настроенный экземпляр с журналом в `data/` и токеном Telegram в переменной `TELEGRAM_BOT_TOKEN` или файле `bot-token.txt`.

## Как выпускать правки

Отправь изменения кода в ветку `main`. GitHub Actions соберёт Python-файлы из корня репозитория и проверит запуск на временной базе. Затем он загрузит сборку на выбранный сервер, сверит её байты, запишет точку входа и запросит перезапуск контейнера. Во время запуска возможна короткая пауза. Изменения одного README пропускают обновление.

Журнал, участники, оценки и токен хранятся на сервере. При повторном запуске бот открывает ту же базу. Сборки сохраняются в корне сервера как `bek264-<хеш>.pyz`. Для возврата предыдущего кода отмени нужный коммит через `git revert` и отправь изменения в `main`. Откат кода сам по себе не отменяет изменений схемы базы.

[Настройка ключей Altare](https://docs.altare.gg/altare-api/tokens) · [Файлы сервера](https://docs.altare.gg/altare-api/files) · [Управление контейнером](https://docs.altare.gg/altare-api/apps)

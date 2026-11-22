# Инфраструктура — этап 5.6.3

## Архитектура и границы

Используется существующий `compose.yaml`, а не второй независимый стек.

| Сервис | Назначение | Хранение / доступ |
|---|---|---|
| app | Django 6.0.10 / Python 3.14.2 / Gunicorn, UID 10001 | Только внутренняя сеть, порт 8000 |
| db | PostgreSQL 17.12, вся бизнес-информация и сессии | Постоянный `postgres_data`; нет host-порта по умолчанию |
| redis | Стандартный Django RedisCache, временные некритичные значения | Без персистентности, maxmemory 96 MB, нет host-порта |
| proxy | Непривилегированный Nginx: HTTP/HTTPS, static, media | Только выбранные HTTP(S)-порты; тома read-only |

Nginx — дополнительный сервис для раздачи media и TLS без development server.
`backend` — внутренняя сеть; только proxy также подключён к `frontend`.
Каждый сервис имеет healthcheck, restart policy и лимит CPU/памяти.
App запускается после healthcheck PostgreSQL/Redis, повторно проверяет SQL и
отсутствие неприменённых миграций, выполняет collectstatic и через exec запускает
Gunicorn. PID/signals обслуживаются init/Gunicorn, окно завершения — 30 секунд.
Миграции при старте web НЕ применяются автоматически.

Образ собирается в две стадии из wheels; компилятор не нужен. Runtime — non-root,
read-only filesystem с отдельными writable static/media и /tmp. `.dockerignore`
разрешает только исходники приложения и зависимости: .env, venv, Git, локальные
БД, media, сертификаты и backup не передаются в контекст. Никаких build-secret/ARG
с паролями нет. Тестовые settings содержат только публичные фикстуры и не являются
настройками production. Теги образов фиксируют версии; для строгой идентичности
между датами релиза дополнительно фиксируйте проверенные registry digests.

Redis подключён как `default` Django cache; сейчас используется readiness-проверкой
для set/get/delete с TTL. Бизнес-ответы и права намеренно не кешируются: это не
изменяет правила актуальности бронирований. Sessions остаются в PostgreSQL.
Нет Celery, очередей и переносов revision/идемпотентности/аудита в Redis.
Остановка Redis даёт `cache=unavailable` в `/health/`, но не запрещает SQL-операции.
Недоступная БД даёт HTTP 503. Это проверки готовности, не система мониторинга.

## Требования

- Docker Desktop (Windows: WSL2, Linux containers), Compose **2.24.4+** (`!override`).
- Ориентир для тестового стенда: 4 GB свободной RAM, место для образов и томов.
- Для venv: Python 3.14.2, Git; Gunicorn на Windows не устанавливается.
- Доступ к Docker Hub и PyPI для первой сборки. Последующие сборки используют кеш.
- Свободные порты: тестовый HTTP 18063; локальный DB override 55440; production —
  явно выбранные HTTP/HTTPS. Не останавливайте чужой контейнер для освобождения порта.

## Существующие установки

Имя `postgres_data` сохранено, но host-публикация DB перенесена в
`compose.local.yaml`. Этот этап НЕ переключает существующую рабочую установку.
Не выполняйте `up` с её именем проекта без отдельного плана: изменённый Compose
может пересоздать её контейнер. Никогда не меняйте POSTGRES_USER/DB/PASSWORD,
ожидая, что это изменит уже инициализированный том: переменные init работают
только на пустом хранилище. Не удаляйте том для исправления ошибки пароля.

## Изолированный стенд: PowerShell

Все команды ниже с `mrb-infra-test` относятся только к новой тестовой установке.
`docker/test.env` — публичные фикстуры. Флаг `--env-file` исключает автоматическое
чтение рабочего .env самим Compose; приложение получает `DJANGO_READ_DOT_ENV=false`.
Не задавайте одновременно POSTGRES_*/DJANGO_* в оболочке: окружение оболочки имеет
приоритет над env-file. Сначала проверьте `docker ps -a` и назначение томов.

```powershell
$dc = @('--env-file', 'docker/test.env', '-p', 'mrb-infra-test', '-f', 'compose.yaml', '-f', 'compose.test.yaml')
docker compose @dc config --quiet
docker compose @dc build --no-cache app
docker compose @dc up -d --wait db redis
docker compose @dc run --rm --no-deps app python manage.py migrate --noinput
docker compose @dc up -d --wait --wait-timeout 120
docker compose @dc ps
```

Открыть http://127.0.0.1:18063/. Для создания административного пользователя:

```powershell
docker compose @dc exec app python manage.py createsuperuser
docker compose @dc exec app python manage.py setup_roles
docker compose @dc logs --tail=100 app proxy
docker compose @dc stop
docker compose @dc up -d --wait --no-recreate
```

`run --rm` удаляет только собственный одноразовый контейнер команды. `stop`
сохраняет все сервисные контейнеры и тома. `down -v`, prune и удаление чужих
контейнеров не используются. Пересборка `build app` сама по себе данные не меняет.
Для применения нового образа нужен согласованный `up -d app proxy`, который
может пересоздать app-контейнер, но использует те же volumes. Перед миграциями
непустой установки остановить писателей, сделать и проверить backup.

## Локальная разработка Windows / venv

Существующий парсер .env сохранён; при наличии переменных оболочки они приоритетны.
Для запуска без чтения .env используйте переменные процесса:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
$env:DJANGO_READ_DOT_ENV = 'false'
$env:DJANGO_ENV = 'development'
$env:DJANGO_DEBUG = 'True'
$env:DJANGO_ALLOWED_HOSTS = 'localhost,127.0.0.1'
$env:DJANGO_SECRET_KEY = python -c "import secrets; print(secrets.token_urlsafe(64))"
# Для новой локальной SQLite установки; проверьте, что db.sqlite3 не содержит чужих данных.
$env:DB_ENGINE = 'sqlite'
python manage.py migrate --plan
# migrate выполнять только после проверки целевой локальной БД.
python manage.py migrate
python manage.py runserver 8001
```

Для локальной PostgreSQL вместо SQLite можно поднять **новый** проект с
`compose.yaml + compose.local.yaml` и затем задать POSTGRES_HOST=127.0.0.1,
POSTGRES_PORT=55440, POSTGRES_DB/USER/PASSWORD согласно этому проекту.
Значения прежнего подключения в settings по умолчанию (meetingroom, localhost:5432)
не изменены; не используйте их в тестах. Redis для venv необязателен; при пустом
REDIS_URL применяется стандартный локальный cache Django. Пароли не печатать.

## Production / HTTPS

`.env.example` — шаблон без секретов. Скопируйте его только если целевой файл ещё
не существует; заполните значения в защищённом редакторе. Существующий .env не
перезаписывать. Генерируйте уникальные SECRET_KEY и POSTGRES_PASSWORD; публичные
значения из docker/test.env недопустимы. Для CI секреты не требуются.

Настройки принимают существующие DJANGO_SECRET_KEY, DJANGO_DEBUG, DB_ENGINE,
POSTGRES_PASSWORD, а также POSTGRES_HOST/PORT/DB/USER, DJANGO_ALLOWED_HOSTS,
DJANGO_CSRF_TRUSTED_ORIGINS, REDIS_URL, DJANGO_STATIC_ROOT, DJANGO_MEDIA_ROOT.
Production запрещает DEBUG=True, короткий/insecure SECRET_KEY, wildcard hosts,
SQLite и отключение secure cookies/HTTPS redirect. Для локального HTTP используется
только development override. CSRF origins должны содержать схему и внешний порт.

Подготовьте отдельный защищённый env-file (не Git) и TLS-файлы:
`docker/tls/fullchain.pem`, `docker/tls/privkey.pem`. Ключ должен быть доступен
пользователю nginx внутри контейнера для чтения, но не посторонним пользователям.
Каталог исключён из Git и образа. Выпуск/продление сертификатов выполняет оператор;
в проекте не добавлены ACME и автоматическая публикация в интернет.

```powershell
$prod = @('--env-file', '<protected-env-file>', '-p', '<new-production-project>', '-f', 'compose.yaml', '-f', 'compose.production.yaml')
docker compose @prod config --quiet
docker compose @prod build app
docker compose @prod up -d --wait db redis
docker compose @prod run --rm --no-deps app python manage.py check --deploy --fail-level WARNING
docker compose @prod run --rm --no-deps app python manage.py migrate --plan
# После проверки НОВОЙ базы / согласования и backup существующей:
docker compose @prod run --rm --no-deps app python manage.py migrate --noinput
docker compose @prod up -d --wait
```

По умолчанию HTTPS доступен только на loopback:8443; для публичного развёртывания
оператор явно выбирает HTTPS_BIND и HTTPS_PORT=443 (HTTP_PORT=80 для внешнего
reverse proxy). HTTP redirect указывает стандартный HTTPS порт 443. Для локальной
проверки 8443 открывайте HTTPS URL напрямую. Include localhost в ALLOWED_HOSTS
не обязателен для app-healthcheck: он использует первый разрешённый host.
Nginx заменяет X-Forwarded-Proto, app-порт не опубликован; только в этой доверенной
сети включён DJANGO_TRUST_PROXY. Произвольный клиентский заголовок не доверяется.

Static собираются в static_data при запуске, media пишутся в отдельный media_data.
Nginx читает оба тома. HTML/SVG upload не исполняется: неизвестные типы выдаются как
octet-stream, nosniff и sandbox CSP; обычные изображения отображаются как прежде.
Media не является конфиденциальным хранилищем: существующие фотографии комнат публичны.
Redis без пароля доступен только из доверенной внутренней сети; не публикуйте его
порт и не подключайте посторонние контейнеры. PostgreSQL bootstrap role владеет БД;
выделение отдельных runtime/migration DB-ролей требует отдельного эксплуатационного плана.

## Проверки

```powershell
python manage.py test --settings=config.test_settings --noinput
python manage.py test --settings=config.test_postgres_settings --noinput
python manage.py check --settings=config.test_settings
python manage.py makemigrations --check --dry-run --settings=config.test_postgres_settings
python -m pip check
python -m ruff check .
python scripts/check_format.py
git diff --check
python scripts/infra_probe.py
python scripts/verify_infrastructure.py
docker compose @dc exec -T app python manage.py test --settings=config.test_container_settings --noinput
docker compose @dc exec -T app python manage.py test --settings=config.test_settings --noinput
```

Host PostgreSQL settings жёстко ограничены 127.0.0.1:55439; container settings —
db:5432 / booking_tests. Не запускайте container test settings в рабочем проекте.
Тестовые БД создаёт и удаляет Django runner; существующие данные приложения остаются.
`verify_infrastructure.py` разрешает только mrb-infra-test/mrb-ci, дополнительно
проверяет имя/пользователя/host БД и development-режим. Он создаёт две тестовые
брони с ключами/событиями, останавливает Redis и DB, проверяет idempotent replay,
сохранность снимков, повторный запуск и восстановление. Тестовые записи сохраняются.
Нельзя запускать его против production. Это функциональные проверки отказов,
не нагрузочное тестирование.

## CI/CD

Дополнение для сдачи: coverage измеряет statements и branches. CI требует минимум
70% для модулей из scripts/check_coverage.py и запускает HTTP E2E без Django test
client. Docker target `test` добавляет coverage, а production target не включает
этот инструмент. `.coveragerc` исключает тесты, миграции и entrypoints из процентного
знаменателя; миграции проверяются отдельными интеграционными тестами.

`.github/workflows/ci.yml`: push и pull_request, read-only GitHub permissions,
Python/pip cache, PostgreSQL service 17.12, Redis service. Обязательны check,
makemigrations, оба полных набора тестов, ruff, formatting, diff, сборка образа,
HTTP smoke, проверки отказов/сохранности и PostgreSQL-тесты внутри образа.
Сборка использует BuildKit GitHub cache. Сначала собирается test target, который
используется для интеграционных проверок; после успешных проверок отдельно
собирается production target. Образ экспортируется в
`meetingroom-production-image.tar` и прикладывается к workflow как artifact
`meetingroom-production-image` со сроком хранения 30 дней. Скачать его можно в
разделе Artifacts успешного GitHub Actions run, затем импортировать командой
`docker load -i meetingroom-production-image.tar`. Это сборка runtime образа,
а не публикация в постоянный registry. Ошибка обязательного шага останавливает job;
finally останавливает только собственный тестовый проект.

Ruff проверяет синтаксические/семантические ошибки во всём проекте (E9/F63/F7/F82).
Format gate охватывает новые инфраструктурные Python-файлы; прежние файлы не
переформатированы. Это сознательное ограничение охвата, а не заявление, что весь
исторический код соответствует стилю Ruff. Настройте branch protection на оба jobs.
CI не требует пользовательских GitHub Secrets. Автодеплой на Render и публикация
внешнего registry не включены: Render Auto Deploy выключен, credentials/политика
выпуска не согласованы. GitHub Actions при этом выполняет CI и сохраняет скачиваемый
production image artifact.

## Backup / restore

Полная политика: [MIGRATION_SAFETY.md](MIGRATION_SAFETY.md). Перед опасными
операциями остановить писателей, зафиксировать код/миграции, проверить backup
восстановлением на новой пустой тестовой БД. Не пытаться обойти reverse guards.
Пример только для изолированного стенда, без бинарного PowerShell pipeline:

```powershell
New-Item -ItemType Directory -Force backups | Out-Null
docker compose @dc stop app proxy
docker compose @dc exec -T db pg_dump -U booking_tests -d booking_tests -Fc -f /tmp/booking-tests.dump
docker compose @dc cp db:/tmp/booking-tests.dump ./backups/booking-tests.dump
Get-FileHash ./backups/booking-tests.dump -Algorithm SHA256
docker compose @dc exec -T db pg_restore --list /tmp/booking-tests.dump
# Выбрать новое имя; createdb должен ОТКАЗАТЬ, если такая БД уже существует.
docker compose @dc exec -T db createdb -U booking_tests booking_restore_check
docker compose @dc exec -T db pg_restore -U booking_tests -d booking_restore_check --exit-on-error --single-transaction /tmp/booking-tests.dump
docker compose @dc up -d --wait --no-recreate
```

Не выполнять restore поверх действующей БД, не использовать --clean и не удалять
исходный том. Проверить строки Booking/BookingCreationRequest/AuditEvent, FK,
revision, sequence, exclusion constraint, trigger и django_migrations. Media_data
резервировать отдельно согласованным файловым backup; один pg_dump фото не содержит.
Для production также сохранить роли, права, TLS и конфигурацию в защищённом
хранилище. `.gitignore` — не механизм защиты backup: ограничьте доступ и храните
копии вне компьютера. Эксплуатационный restore production в этом этапе не выполняется.

## Диагностика

- `config --quiet` сообщает отсутствие обязательной переменной: заполните именно
  выбранный env-file; не выводите полный `compose config` с секретами в лог.
- App не стартует до `migrate`: ожидаемый отказ, миграции выполняются оператором.
- 400 DisallowedHost: проверьте внешний host в DJANGO_ALLOWED_HOSTS.
- CSRF 403: внешний HTTPS origin/порт и DJANGO_CSRF_TRUSTED_ORIGINS должны совпасть.
- Redirect loop: не смешивать production с HTTP override; проверять TLS и доверенный proxy.
- Redis unhealthy: проверить его logs и сеть; не удалять SQL-данные и ключи создания.
- DB unhealthy: проверить readiness и выбранный том. Нельзя менять пароль env и
  удалять том «для переинициализации».
- Docker Engine временно недоступен: дождаться готовности Docker Desktop, не создавать
  запасное подключение к рабочему PostgreSQL.
- Port in use / чужой project: остановиться и согласовать другое имя/порт.
- `stop` оставляет данные; healthcheck сам не перезапускает живой unhealthy-процесс.
  Restart policy перезапускает завершившийся процесс. После восстановления DB
  CONN_HEALTH_CHECKS позволяет Django открыть новое соединение.


## Локальный Grafana dashboard и k6 load test

Эти сервисы предназначены только для изолированного Docker Compose стенда.
Не подключайте их к Render, Neon, production DB или публичному адресу.
Мониторинговые порты привязаны к 127.0.0.1; k6 обращается к proxy внутри Docker-сети.

### Запуск observability

```powershell
$dc = @('--env-file', 'docker/test.env', '-p', 'mrb-infra-test', '-f', 'compose.yaml', '-f', 'compose.test.yaml')
$mon = @('--env-file', 'docker/test.env', '-p', 'mrb-infra-test', '-f', 'compose.yaml', '-f', 'compose.test.yaml', '-f', 'compose.monitoring.yaml')
docker compose @dc up -d --wait db redis
docker compose @dc run --rm --no-deps app python manage.py migrate --noinput
docker compose @dc up -d --wait
docker compose @mon up -d --wait prometheus alertmanager grafana
python scripts/verify_monitoring.py --project mrb-infra-test
```

Открыть локально:
- Grafana: http://127.0.0.1:18091 (admin / isolated-grafana-only — только локальный demo-пароль).
- Prometheus: http://127.0.0.1:18090.
- Alertmanager: http://127.0.0.1:18093.

Dashboard **MeetingRoomBooking — Operations** provisioned автоматически. Он показывает
RPS, HTTP 5xx, p95/p99, PostgreSQL connections/active/max и состояние PostgreSQL/Redis.
Метрики DB connections читают pg_stat_activity только для PostgreSQL; usernames,
booking IDs и query strings в labels не попадают. Перед production use сменить
пароль Grafana, bearer token scrape, закрыть UI от публичного доступа и настроить
внешний receiver отдельно. Email/webhook не настроены.

### Испытание 1000 виртуальных пользователей

Тест k6 выполняет только GET к каталогу и публичному списку комнат: 80% API и 20% HTML.
Один VU делает примерно один запрос каждые 8–12 секунд. Профиль постепенно повышает
нагрузку до 1000 VU, держит пик 60 секунд и затем завершает работу. Это модель около
100 RPS при 1000 активных виртуальных сессиях, а не 1000 одновременных SQL-записей
и не тест бронирования. Порог успешности: HTTP errors <1%, p95 <1 секунды.

```powershell
$load = @('--env-file', 'docker/test.env', '-p', 'mrb-infra-test', '-f', 'compose.yaml', '-f', 'compose.test.yaml', '-f', 'compose.monitoring.yaml', '-f', 'compose.load.yaml')
docker compose @load run --rm k6
```

После завершения k6 сохранит сводку в `reports/k6-summary.json`. Зафиксируйте
реальные VUs, achieved RPS, error rate, p95/p99 и CPU/RAM Docker. Если пороги
нарушены, результат считается неуспешным; не переписывайте его как успешный.
Тест не запускать на Render/Neon и не направлять на пользовательский production-сайт.

Результат CI run №59 (GitHub Actions, изолированный Docker Compose): достигнуто
1000 VU на 60-секундном плато, 10 563 GET-запроса, 0 HTTP-ошибок,
10 562/10 562 успешных checks, средний RPS за весь профиль 61,8,
p95 9,18 мс, p99 29,56 мс, максимум 205,65 мс. Сводка доступна как artifact
в [workflow run №59](https://github.com/nameforreal/MeetingRoomBooking/actions/runs/44780885738).
Это не подтверждает нагрузку записи бронирований, длительную смешанную нагрузку
или производительность Render; локальный повтор нужен для измерения CPU/RAM
конкретной машины.

Остановка сохраняет контейнеры и тома:

```powershell
docker compose @load stop
```

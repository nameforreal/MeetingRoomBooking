# MeetingRoomBooking

Система бронирования переговорных: Django 6.0.10, Python 3.14.2, PostgreSQL 17.
Поддерживает Permissions/Groups, VIP-согласование, revision, идемпотентное создание,
защищённую историю, аудит и CSV-экспорт по комнате и датам.

## Текущее состояние — 22 ноября 2026

- Реализованы бронирования, VIP-согласование, аудит, CSV, OpenAPI и разграничение доступа; отдельная модель `rooms.Role` хранит код/описание и связана с Django `auth.Group`, которая остаётся источником permissions.
- GitHub Actions проверяет push/PR: тесты, Ruff, форматирование, PostgreSQL, HTTP E2E, Docker, Grafana/Alertmanager и k6.
- Успешный CI run №59 подтвердил 1000 VU в изолированном read-only профиле: 10 563 GET-запроса, 0 HTTP-ошибок, p95 9,18 мс. Это не тест записи бронирований и не нагрузка на Render.
- Production Docker image собирается отдельным target и прикладывается к успешному workflow как скачиваемый artifact `meetingroom-production-image` (30 дней).
- Публичный сервис Render развёрнут вручную; владелец подтвердил 11/11 сценариев ручной приёмки 20.11.2026. Автоматический deploy в Render не настроен.
- Подробная матрица требований, результаты испытаний и ограничения — в [DELIVERY.md](DELIVERY.md).
- UML-схемы — в [ARCHITECTURE.md](ARCHITECTURE.md); реестр дефектов и ручной приёмки — в [DEFECT_REGISTER.md](DEFECT_REGISTER.md).


## Установка

Полная инструкция для Windows, Docker, HTTPS, CI и backup/restore:
[INFRASTRUCTURE.md](INFRASTRUCTURE.md).

Для **нового изолированного стенда** (Docker Desktop, Linux containers, Compose 2.24.4+):

```powershell
$dc = @('--env-file', 'docker/test.env', '-p', 'mrb-infra-test', '-f', 'compose.yaml', '-f', 'compose.test.yaml')
docker compose @dc build app
docker compose @dc up -d --wait db redis
docker compose @dc run --rm --no-deps app python manage.py migrate --noinput
docker compose @dc up -d --wait
```

Сайт: http://127.0.0.1:18063/. Порт должен быть свободен. Публичные значения
`docker/test.env` разрешены только для тестов. Не используйте имя рабочего проекта.

Облачный профиль Render Free + Neon описан в [HOSTING_RENDER.md](HOSTING_RENDER.md).
Он использует отдельный Dockerfile и не переключает локальный Compose на облако.

## IDE (VS Code)

Рекомендуемые расширения и настройки Ruff, Docker, GitLens и YAML хранятся
в `.vscode/extensions.json` и `.vscode/settings.json`. VS Code предложит
установить расширения при открытии проекта; Python interpreter выбирается
из активного виртуального окружения.

## Windows venv и проверки

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
python manage.py test --settings=config.test_settings --noinput
python manage.py check --settings=config.test_settings
python -m ruff check .
python scripts/check_format.py
git diff --check
```

Запуск полного PostgreSQL-набора в изолированном контейнере:

```powershell
docker compose @dc exec -T app python -m coverage run manage.py test --settings=config.test_container_settings --noinput
docker compose @dc exec -T app python scripts/check_coverage.py
python scripts/infra_probe.py
python scripts/verify_infrastructure.py
python scripts/e2e_http.py
```

Для Windows runserver и собственного окружения следуйте INFRASTRUCTURE.md.
SQLite не считается доказательством PostgreSQL-блокировок и триггеров.
GitHub Actions проверяет SQLite/PostgreSQL-наборы, HTTP E2E, мониторинг и нагрузочный профиль; также собирает production Docker image и сохраняет его как artifact. Workflow не развёртывает приложение в Render автоматически: текущая публикация выполнена вручную, Auto Deploy выключен.

## Документация

- [TECHNICAL_SPECIFICATION.md](TECHNICAL_SPECIFICATION.md) — краткое ТЗ.
- [PROJECT_PLAN.md](PROJECT_PLAN.md) — этапы, сроки и риски.
- [REQUIREMENTS_CHECKLIST.md](REQUIREMENTS_CHECKLIST.md) — сверка восьми разделов задания.
- [IMPACT_ANALYSIS.md](IMPACT_ANALYSIS.md) — влияние VIP/CSV изменений.
- [ARCHITECTURE.md](ARCHITECTURE.md) — UML component/deployment diagrams.
- [DEFECT_REGISTER.md](DEFECT_REGISTER.md) — дефекты и приёмка.
- [BUSINESS_RULES.md](BUSINESS_RULES.md), [API_GUIDE.md](API_GUIDE.md) — контракты.
- [VIP_APPROVAL.md](VIP_APPROVAL.md), [ROLES.md](ROLES.md) — согласование и права.
- [CONCURRENCY.md](CONCURRENCY.md), [AUDIT_HISTORY.md](AUDIT_HISTORY.md) — история и блокировки.
- [MIGRATION_SAFETY.md](MIGRATION_SAFETY.md), [CSV_EXPORT.md](CSV_EXPORT.md) — безопасность.
- [INFRASTRUCTURE.md](INFRASTRUCTURE.md) — актуальная установка вместо исторических INSTALL/UPDATE.
- [DELIVERY.md](DELIVERY.md) — матрица требований, мониторинг, пилот, ресурсы и реестр ограничений.
- [PROJECT_PLAN.md](PROJECT_PLAN.md) — этапы работ, оценка трудозатрат и риски.
- `/api/schema/` — OpenAPI JSON; `/api/docs/` — Swagger UI (интерфейс требует CDN).

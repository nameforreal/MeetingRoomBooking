# Render Free + Neon: отдельный облачный профиль

Локальные `Dockerfile`, Compose, Nginx, `.env` и стенд на 18063 не переключаются
на облако. Облачный запуск использует `Dockerfile.render` и
`config.settings_render`. Не копируйте рабочий `.env` в Render или Docker build.
Никакие миграции, seed, создание пользователей или ролей не выполняются при
сборке/старте веб-сервиса. Этот документ не является разрешением выполнять записи в Neon.

## Как устроен запуск

Render завершает HTTPS и направляет запросы в Gunicorn на `0.0.0.0:$PORT`
(по умолчанию 10000). Контейнер работает под UID 10001; один worker с двумя
потоками ограничивает потребление памяти и число соединений с Neon.
`docker/render_entrypoint.py` проверяет Django, отсутствие неприменённых миграций,
собирает static и запускает Gunicorn через exec. При ошибке сервер не запускается.
Веб-процесс никогда не применяет миграции.

WhiteNoise раздаёт собранные static с manifest/hash и сжатием. `/health/` проверяет
PostgreSQL; отказ необязательного Redis не делает БД недоступной. Nginx в облаке
не нужен. Access log не содержит query string, cookies, тела или Authorization.
`/metrics/` закрыт при пустом METRICS_TOKEN; Prometheus/Alertmanager автоматически
не разворачиваются. В отличие от локального Nginx, здесь доступ с токеном возможен
через публичный HTTPS — включайте его только при необходимости.

Настройки принудительно отключают DEBUG и чтение `.env`, включают HTTPS redirect,
secure cookies, HttpOnly session cookie, SameSite=Lax и доверие
`X-Forwarded-Proto` от Render. Этот профиль нельзя выставлять напрямую в интернет
мимо доверенного reverse proxy. ALLOWED_HOSTS и CSRF_TRUSTED_ORIGINS выводятся из
конкретного hostname Render и дополнительных явно заданных доменов.

HSTS первоначально действует один час, без includeSubDomains/preload. Поэтому
`check --deploy` сообщает W005 и W021: это намеренное ограничение до проверки
домена, а не отключение HTTPS. После выбора постоянного домена эти параметры
можно пересмотреть отдельно; другие предупреждения нельзя игнорировать.

## Переменные окружения

Значения вводятся в Render Environment / защищённый менеджер секретов, не в Git.

| Переменная | Обязательность и назначение |
|---|---|
| `DJANGO_SECRET_KEY` | Секрет: случайный, не менее 50 символов; рекомендуется 64+. Стабилен между деплоями. |
| `NEON_HOST` | Только Direct hostname нужной ветки, без схемы, порта и учётных данных. |
| `NEON_DATABASE` | Имя базы, для созданного проекта `neondb`. |
| `NEON_USER` | Роль подключения из выбранной ветки Neon. |
| `NEON_PASSWORD` | Секрет: пароль этой роли. |
| `RENDER_EXTERNAL_HOSTNAME` | Render задаёт автоматически. Для локальной проверки облачного профиля задаётся явно. |
| `PORT` | Render задаёт автоматически, fallback 10000. |
| `DJANGO_ALLOWED_HOSTS` | Необязательно: дополнительные домены через запятую, без `*`, схем и портов. HTTPS origins формируются автоматически. |
| `UPSTASH_REDIS_URL` | Необязательный секрет: нативный Redis URL с `rediss://`, не REST URL/token. |
| `METRICS_TOKEN` | Необязательный отдельный случайный ASCII-секрет; пустой отключает доступ. |
| `NEON_SSLROOTCERT` | Необязательно: путь к доверенному CA bundle. В Docker используется системный bundle Debian. Не отключайте проверку. |

`DJANGO_ENV`, `DJANGO_DEBUG`, `DB_ENGINE`, `POSTGRES_*`, старый `REDIS_URL` и
флаги HTTPS отдельно задавать не нужно: облачный профиль устанавливает их сам.
Секреты не передаются в build args. Случайное `generateValue` Render сейчас выдаёт
44-символьную base64-строку, которая не проходит существующее требование проекта
к длине SECRET_KEY; поэтому Blueprint запрашивает секрет явно.
[Спецификация Render](https://render.com/docs/blueprint-spec).

## Neon Direct и PostgreSQL 18

Подключение использует `verify-full`, проверку hostname/CA, `channel_binding=require`
и timeout 10 секунд. `CONN_MAX_AGE=0`: соединение не удерживается между запросами.
Server-side cursors отключены, psycopg prepared statements отключены.
Endpoints с `-pooler` пока отвергаются. Перед будущим включением transaction pooling
нужно отдельно проверить транзакционные advisory locks, разрешение триггера,
идемпотентность и ограничения; команду bootstrap всегда выполнять через Direct.
[Neon TLS](https://neon.com/docs/connect/connect-securely),
[Django и transaction pooling](https://docs.djangoproject.com/en/6.0/ref/databases/#transaction-pooling-and-server-side-cursors).

Существующая миграция 0005 создаёт `btree_gist` и exclusion constraint
`booking_no_active_overlap` для pending/confirmed. 0009 создаёт PL/pgSQL-триггер
защиты User.is_active; используются также `txid_current`, transaction-local
`set_config` и PostgreSQL-блокировки. PostgreSQL 18 поддерживает эти механизмы;
Neon документирует поддержку btree_gist. Это не подтверждает фактические права
роли или успешность миграций на вашей ветке.
[PostgreSQL 18 btree_gist](https://www.postgresql.org/docs/18/btree-gist.html),
[Neon btree_gist](https://neon.com/docs/extensions/btree_gist).

## Безопасная первичная инициализация hosting-test

На Free нет Shell, one-off jobs и платного pre-deploy command. Инициализация
выполняется вручную с доверенного компьютера в том же облачном Docker-образе.
[Ограничения Free](https://render.com/docs/free),
[pre-deploy](https://render.com/docs/blueprint-spec#predeploycommand).

1. Сначала получить отдельное разрешение на подключение/миграции нужной ветки.
2. В Neon Console выбрать **hosting-test**, Direct, `neondb`. Сверить endpoint с
   карточкой именно этой ветки. До подключения SQL команда независимо запрашивает
   официальный Neon API: project, branch и endpoint. Проверяются все ID,
   точное имя ветки hosting-test, отсутствие default/protected и соответствие
   Direct hostname пишущему endpoint этой ветки. Метки окружения не считаются доказательством.
   Любая ошибка API, timeout, redirect, неполный ответ или несовпадение запрещают продолжение.
3. Убедиться, что это новая пустая база. Ветка Neon может быть клоном production:
   не считать её пустой только по имени. Команда откажет при существующих таблицах,
   представлениях или последовательностях, в том числе при уже применённых миграциях.
4. Задать NEON-параметры и SECRET_KEY только в окружении отдельного терминала.
   Не вставлять пароль в командную строку/историю. Например, в PowerShell:

   ```powershell
   $env:NEON_PASSWORD = [System.Net.NetworkCredential]::new('', (Read-Host 'Neon password' -AsSecureString)).Password
   $env:DJANGO_SECRET_KEY = [System.Net.NetworkCredential]::new('', (Read-Host 'Django secret key' -AsSecureString)).Password
   $env:NEON_HOST = Read-Host 'Direct hostname verified in hosting-test branch'
   $env:NEON_DATABASE = 'neondb'
   $env:NEON_USER = Read-Host 'Neon role'
   $env:RENDER_EXTERNAL_HOSTNAME = Read-Host 'Public hostname, without https://'
   $env:NEON_BRANCH = 'hosting-test'
   $env:NEON_EXPECTED_HOST = Read-Host 'Re-enter verified hosting-test Direct hostname'
   $env:NEON_PROJECT_ID = Read-Host 'Neon project ID'
   $env:NEON_BRANCH_ID = Read-Host 'hosting-test branch ID'
   $env:NEON_ENDPOINT_ID = Read-Host 'hosting-test Direct endpoint ID'
   $env:NEON_API_KEY = [System.Net.NetworkCredential]::new('', (Read-Host 'Neon API key' -AsSecureString)).Password
   ```

5. Собрать образ без секретов. Подготовить аргументы, которые передают значения
   из окружения, а не печатают их в командной строке:

   ```powershell
   docker build -f Dockerfile.render -t meetingroom-render:reviewed .
   $cloudEnv = @('--env','NEON_HOST','--env','NEON_DATABASE','--env','NEON_USER',
     '--env','NEON_PASSWORD','--env','DJANGO_SECRET_KEY','--env','RENDER_EXTERNAL_HOSTNAME',
     '--env','NEON_BRANCH','--env','NEON_EXPECTED_HOST','--env','NEON_PROJECT_ID',
     '--env','NEON_BRANCH_ID','--env','NEON_ENDPOINT_ID','--env','NEON_API_KEY')
   docker run -it @cloudEnv meetingroom-render:reviewed python manage.py neon_bootstrap
   ```

   По умолчанию команда выполняет GET к Neon API, затем только SELECT: проверяет
   PostgreSQL 18, TLS, доступность btree_gist, CREATE-права и отсутствие посторонних объектов.
   Проверяются таблицы/представления/последовательности, функции/процедуры, типы,
   схемы, операторы, collations, text search, расширения (кроме штатного plpgsql),
   event triggers, FDW/servers, публикации/подписки, large objects и default ACL.
   Пользовательские схемы, начинающиеся с pgX, больше не ошибочно исключаются.
   Если Neon создаёт дополнительные платформенные объекты, команда отказывает:
   их нельзя удалять или обходить проверку без отдельного анализа.
   Ни расширение, ни таблицы на этом шаге не создаются. Не выводите docker inspect
   Env, `diffsettings`, DSN или исключения драйвера с приватными параметрами.
6. **Только после отдельного согласования применения**:

   ```powershell
   docker run -it @cloudEnv meetingroom-render:reviewed python manage.py neon_bootstrap --apply --confirm-host $env:NEON_EXPECTED_HOST
   ```

   Команда повторяет проверки, берёт advisory lock от второго параллельного
   bootstrap, повторно проверяет Neon API непосредственно перед DDL,
   применяет существующие миграции и проверяет constraint/trigger.
   Это не общая атомарная транзакция всех миграций: при сбое часть могла завершиться.
   Автоматического retry, удаления, отката схемы или очистки нет. Не повторять
   вслепую: сначала проверить состояние отдельно. Повторный bootstrap непустой базы
   намеренно запрещён. Локальные Docker-контейнеры команд содержат секреты окружения;
   выполняйте их только на доверенном компьютере и управляйте ими отдельно.
7. После успешных миграций и разрешения на эти записи отдельно выполнить
   `python manage.py setup_roles` и интерактивный `python manage.py createsuperuser`
   через тот же `docker run -it @cloudEnv ...`. Не использовать стандартный пароль,
   не запускать автоматический seed. Комнаты можно добавить вручную через Admin.
8. Закрыть отдельный терминал с секретами. Не применять `manage.py test` к Neon:
   Django runner создаёт/удаляет базы. Полный набор запускается на локальном
   изолированном PostgreSQL, а на Neon — только согласованные smoke-проверки.

NEON_API_KEY требуется только локальной команде bootstrap: не добавляйте его
в Render web service. Используйте доступный минимально необходимый scope ключа.
API base URL фиксирован, redirects и proxy из окружения отключены, TLS проверяется.
Тексты ответов API и исключений транспорта не выводятся. Проверка опирается на
доверенный Neon control plane; она не защищает от администратора, который одновременно
переименовывает/переназначает ветки. На время инициализации исключите такие изменения.
[Neon endpoint API](https://neon.com/docs/reference/api/endpoints/get-project-endpoint),
[Neon branch API](https://neon.com/docs/reference/api/branches/get-project-branch).

Команда bootstrap не разрешает production по данным API. Реальный переход на
production требует отдельного согласования endpoint, резервной копии и процедуры
миграции. Смена NEON_BRANCH или повторный ввод hostname теперь недостаточны для прохождения проверки.

## Подключение GitHub к Render

После проверки изменений владелец делает commit/push и проверяет CI. Затем:

1. Render → New → Web Service → подключить нужный GitHub repository и ветку.
2. Выбрать **Docker**, `Dockerfile.render`, регион Frankfurt, instance **Free**.
   Root Directory оставить пустым. Docker Command не переопределять.
3. Auto Deploy выключить; Health Check Path — `/health/`. Добавить переменные
   выше для согласованной hosting-test. SECRET_KEY хранить стабильным.
4. Не добавлять Render Postgres, Key Value, disk, cron или платный instance.
   Альтернатива — проверить `render.yaml` и вручную создать Blueprint; первая
   активация Blueprint сама запускает первоначальный deploy, даже с auto-deploy off.
5. Миграции выполнить отдельно по процедуре выше до первого успешного запуска.
   При пустой схеме веб-процесс корректно откажет в старте.
6. Вручную запустить deploy и проверить HTTPS, `/health/`, CSS/изображения,
   регистрацию/login/CSRF, права, создание/replay/отмену и VIP-согласование
   на выделенных тестовых данных. Проверить невозможность upload и чужого доступа.
   Проверить restart: данные/сессии сохраняются в Neon, static собирается заново.

Render Free засыпает после 15 минут без трафика, имеет холодный старт, не предоставляет
постоянный диск и может перезапускаться. Не обещать SLA и не создавать искусственный
keep-alive ради обхода лимитов. Для первых проверок Upstash и Cloudinary не нужны.
[Render Free](https://render.com/docs/free), [PORT](https://render.com/docs/web-services).

## Media и следующий шаг Cloudinary

Для пяти комнат подготовлена отдельная команда `seed_hosting_rooms --manifest PATH`.
Без --apply она только проверяет и планирует; с --apply создаёт недостающие комнаты
в одной транзакции, с блокировкой таблицы от параллельных записей. Старый seed не менялся.
Повтор совпадающего manifest ничего не создаёт; дубли названий/расхождения вызывают
отказ без перезаписи, другие комнаты не удаляются. Поэтому итоговое число ВСЕХ комнат
может быть больше пяти, если они уже существовали.

Manifest — JSON-массив ровно пяти объектов с именами «Переговорная №1» — «Переговорная №5».
Каждый объект должен явно содержать `name`, `capacity` (1–200), `is_vip` (boolean),
`is_active` (boolean), `description` (строка, допустима пустая), `photo` (пока только null).
Готового manifest нет: параметры ещё не согласованы. Defaults Room — capacity=1,
is_vip=false, is_active=true — не подставляются командой вместо решения владельца.
ID/created_at назначает Django. Фотографии необходимо согласовать отдельно вместе
с постоянным хранилищем; static fallback не означает перенос демонстрационных фото.

Проверка исправлений без миграций: `python -B scripts/check_neon_guards.py`.
Требуется НОВАЯ пустая локальная PostgreSQL 18 на 127.0.0.1:55488 с тестовыми
параметрами config.test_postgres_settings. Скрипт проверяет версию, имя/роль и пустоту.
Только test fixtures создаются SQL/SchemaEditor внутри откатываемых транзакций;
MigrationExecutor и migrate не запускаются. Не направляйте скрипт на Neon.
31 тест прошёл на PostgreSQL 18 в отдельном tmpfs-контейнере без volumes;
API полностью имитировался, реальной проверки Neon ещё нет.

Сейчас в облаке можно создавать комнаты без фото: встроенные изображения находятся
в static. Upload возвращает понятный HTTP 400; default storage также запрещает
запись/удаление из ORM. Импортированные имена photo показывают статическую заглушку,
не пытаются читать локальный диск. Локальный Compose продолжает хранить media в volume.

Следующая отдельная интеграция: Cloudinary Free, проверенный Django storage backend,
секреты cloud name/API key/API secret, HTTPS URL, проверка размера/типа изображений,
ограничение upload только уполномоченным сотрудникам. После тестов заменить default
storage и убрать cloud-only запрет upload. Существующие фото переносить отдельно с
инвентаризацией/резервной копией; не переписывать поля и не удалять оригиналы автоматически.
WhiteNoise предназначен только для static, не для пользовательских файлов.
[WhiteNoise](https://whitenoise.readthedocs.io/en/stable/django.html).

## Необязательный Upstash

Первый старт использует процессный disposable cache. Сессии, Permissions,
идемпотентность и блокировки находятся в PostgreSQL и от cache не зависят.
Для Upstash выбрать Free и близкий регион, передать нативный TLS Redis URL в
UPSTASH_REDIS_URL. Проверка сертификата/hostname обязательна, query-параметры,
способные ослабить TLS, запрещены. Используется DB 0. REST credentials не подходят.
Отдельно проверить доступность и расход лимита команд: health/metrics тоже обращаются
к cache. Без потребности в общем cache оставьте Upstash неподключённым.
[Upstash Redis clients](https://upstash.com/docs/redis/howto/connect-client).

## Границы проверки

Проверено локально 19 ноября 2026, исходная точка `4d4b070`:

- Окончательный Render-образ успешно собран; локальный Docker target `test` тоже собран.
- Полный набор из Render-образа на отдельной PostgreSQL **18.7**: **448/448**, без пропусков.
- Полный набор из того же образа на SQLite в памяти с отключённой сетью:
  **381 выполнен, 67 PostgreSQL-специфичных пропущены**, ошибок нет.
- Добавлены 22 теста облачного профиля, media/static, bootstrap и startup.
- Настоящий Gunicorn `--check-config` с облачным профилем и синтетическими параметрами прошёл без сети.
- WhiteNoise: collectstatic, manifest/hash, HTTP-раздача CSS и immutable cache headers проверены.
- Django check, makemigrations --check --dry-run, pip check, Ruff, format gate,
  YAML syntax/safety assertions и git diff --check прошли. Deployment check имеет
  только намеренные W005/W021, описанные выше.
- PostgreSQL-контейнер задачи: `mrb-render-pg18-20261119`, loopback-порт 55487,
  отдельный volume `mrb-render-pg18-20261119_data`. Runner создавал и удалял только
  свои тестовые БД, в финальном прогоне — `test_render_image_isolated`.
  Базовая `booking_tests` не заполнялась прикладными данными.
- Существующие демонстрационные/рабочие базы и volumes не изменялись.
  В Neon подключения и миграции не выполнялись; Render deployment не запускался.

Изолированные тесты `config.test_render` используют синтетические параметры,
запрещают сеть при импорте профиля, проверяют static HTTP и защитные условия bootstrap.
Моки bootstrap не доказывают работу миграций на Neon. Совместимость PostgreSQL 18
нужно подтверждать отдельным реальным локальным прогоном, а Neon — после разрешения.
Render ingress, сертификат конкретного Neon endpoint, права Neon-роли, Upstash и
Cloudinary нельзя считать проверенными без реального согласованного подключения.

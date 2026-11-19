# syntax=docker/dockerfile:1
ARG PYTHON_IMAGE=python:3.14.2-slim-bookworm
FROM ${PYTHON_IMAGE} AS dependencies
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
COPY requirements.txt .
# All dependencies have wheels; no compiler or headers are needed in runtime.
RUN --mount=type=cache,target=/root/.cache/pip pip wheel --only-binary=:all: --wheel-dir=/wheels -r requirements.txt

FROM dependencies AS test-dependencies
COPY requirements-test.txt .
RUN --mount=type=cache,target=/root/.cache/pip pip wheel --only-binary=:all: --wheel-dir=/test-wheels -r requirements-test.txt

FROM ${PYTHON_IMAGE} AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app DJANGO_READ_DOT_ENV=false
WORKDIR /app
COPY --from=dependencies /wheels /wheels
COPY requirements.txt .
RUN pip install --no-cache-dir --no-index --find-links=/wheels -r requirements.txt \
    && rm -rf /wheels \
    && groupadd --gid 10001 app && useradd --uid 10001 --gid app --no-create-home app \
    && mkdir /app/staticfiles /app/media && chown app:app /app/staticfiles /app/media
COPY manage.py .
COPY config ./config
COPY rooms ./rooms
COPY management ./management
COPY dashboard ./dashboard
COPY docker/entrypoint.py docker/healthcheck.py docker/gunicorn.conf.py docker/render_entrypoint.py docker/render_gunicorn.py ./docker/
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=5s --start-period=30s --retries=6 CMD ["python", "docker/healthcheck.py"]
ENTRYPOINT ["python", "docker/entrypoint.py"]
CMD ["gunicorn", "config.wsgi:application", "--config=docker/gunicorn.conf.py", "--bind=0.0.0.0:8000", "--workers=2", "--timeout=30", "--graceful-timeout=20", "--access-logfile=-", "--error-logfile=-", "--forwarded-allow-ips=*"]

FROM runtime AS test
USER root
COPY --from=test-dependencies /test-wheels /test-wheels
RUN pip install --no-cache-dir --no-index --find-links=/test-wheels coverage==7.17.3 && rm -rf /test-wheels
COPY .coveragerc .
COPY scripts/check_coverage.py ./scripts/
ENV COVERAGE_FILE=/tmp/.coverage
USER 10001:10001

FROM runtime AS production

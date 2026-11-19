"""One small Render Free instance, behind Render's HTTPS ingress only."""

import os

from config.render_config import gunicorn_bind

bind = gunicorn_bind(os.environ)
workers = 1
worker_class = "gthread"
threads = 2
timeout = 60
graceful_timeout = 25
accesslog = "-"
errorlog = "-"
# Do not log query strings, cookies, Authorization or request bodies.
access_log_format = "%(m)s %(U)s %(s)s %(L)s"
forwarded_allow_ips = "*"
secure_scheme_headers = {"X-FORWARDED-PROTO": "https"}


def child_exit(server, worker):
    from prometheus_client import multiprocess

    multiprocess.mark_process_dead(worker.pid)

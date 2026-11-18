"""Enforce >=70% statement+branch coverage of explicitly named critical modules."""

import coverage

CRITICAL = [
    "*/rooms/booking_operations.py",
    "*/rooms/booking_idempotency.py",
    "*/rooms/booking_rules.py",
    "*/rooms/account_operations.py",
    "*/rooms/audit.py",
    "*/rooms/retention.py",
    "*/rooms/permissions.py",
    "*/rooms/querysets.py",
    "*/rooms/models.py",
]

if __name__ == "__main__":
    cov = coverage.Coverage()
    cov.load()
    result = cov.report(include=CRITICAL)
    raise SystemExit(0 if result >= 70 else 1)

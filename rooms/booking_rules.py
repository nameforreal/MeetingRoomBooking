"""Business calendar shared by validation, schedules and reporting."""
import re
from datetime import date, datetime, time, timedelta, timezone as datetime_timezone

from django.core.exceptions import ValidationError
from django.utils import timezone

OPEN_HOUR = 8
CLOSE_HOUR = 20
MIN_DURATION = timedelta(hours=1)
MAX_DURATION = timedelta(hours=4)
BOOKING_HORIZON_DAYS = 30
SLOT_HOURS = range(OPEN_HOUR, CLOSE_HOUR)
# Leave room for timezone conversion and computing the following midnight.
MIN_DATE = date.min + timedelta(days=1)
MAX_DATE = date.max - timedelta(days=1)


def site_today():
    return timezone.localtime(timezone.now(), timezone.get_default_timezone()).date()


def last_booking_date():
    return date.fromordinal(min(site_today().toordinal() + BOOKING_HORIZON_DAYS, MAX_DATE.toordinal()))


def parse_calendar_date(value, *, default=None, minimum=MIN_DATE, maximum=MAX_DATE):
    if value is None and default is not None:
        return default
    try:
        if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
            raise ValueError
        result = date.fromisoformat(value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValidationError("Укажите существующую дату в формате YYYY-MM-DD.") from exc
    if not minimum <= result <= maximum:
        raise ValidationError(f"Дата должна быть в диапазоне {minimum.isoformat()} — {maximum.isoformat()}.")
    return result


def booking_date(value, *, default=None):
    return parse_calendar_date(value, default=default, minimum=site_today(), maximum=last_booking_date())


def day_bounds(selected):
    if not MIN_DATE <= selected <= MAX_DATE:
        raise ValidationError("Дата выходит за допустимые границы календаря.")
    zone = timezone.get_default_timezone()
    try:
        return (timezone.make_aware(datetime.combine(selected, time.min), zone),
                timezone.make_aware(datetime.combine(selected + timedelta(days=1), time.min), zone))
    except (OverflowError, ValueError) as exc:
        raise ValidationError("Невозможно вычислить границы выбранной даты.") from exc


def validate_interval(start, end, *, scheduling=True):
    try:
        if not isinstance(start, datetime) or not isinstance(end, datetime) or timezone.is_naive(start) or timezone.is_naive(end):
            raise ValueError
        zone = timezone.get_default_timezone()
        local_start, local_end = start.astimezone(zone), end.astimezone(zone)
        if not MIN_DATE <= local_start.date() <= MAX_DATE or not MIN_DATE <= local_end.date() <= MAX_DATE:
            raise ValueError
        duration = end.astimezone(datetime_timezone.utc) - start.astimezone(datetime_timezone.utc)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValidationError("Укажите допустимые дату, время и часовой пояс.") from exc
    if duration <= timedelta(0):
        raise ValidationError("Время окончания должно быть позже начала.")
    if not MIN_DURATION <= duration <= MAX_DURATION:
        raise ValidationError("Бронирование должно длиться от 1 до 4 часов.")
    if not scheduling:
        return
    if start < timezone.now():
        raise ValidationError("Нельзя бронировать время в прошлом.")
    if local_start.date() > last_booking_date() or local_end.date() > last_booking_date():
        raise ValidationError("Бронирование доступно не более чем на 30 дней вперёд.")
    if (local_start.date() != local_end.date() or local_start.time() < time(OPEN_HOUR)
            or local_end.time() > time(CLOSE_HOUR)):
        raise ValidationError("Встреча должна проходить с 08:00 до 20:00 в часовом поясе сайта.")

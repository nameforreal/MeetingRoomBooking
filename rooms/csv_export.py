"""Shared spreadsheet-safe CSV format; source database values are never changed."""
import csv
import re

from django.http import HttpResponse
from django.utils import timezone


def safe_csv_cell(value):
    if isinstance(value, str) and (
        value.startswith(("\t", "\r", "\n"))
        or re.match(r"^[\s\x00-\x1f\ufeff]*[=+@-]", value)
    ):
        return "'" + value
    return value


def export_bookings(queryset, *, include_created=False):
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="meetingroom-bookings.csv"'
    response.write("\ufeff")  # Exactly once, not once per writerow().
    writer = csv.writer(response, delimiter=";")
    headers = ["ID", "Переговорная", "Пользователь", "Email", "Начало", "Окончание", "Статус"]
    if include_created:
        headers.append("Создано")
    writer.writerow(headers)
    for booking in queryset.select_related("room", "user").order_by("start_time", "pk").iterator():
        row = [booking.pk, booking.room.name, booking.user.username, booking.user.email,
               timezone.localtime(booking.start_time).strftime("%d.%m.%Y %H:%M"),
               timezone.localtime(booking.end_time).strftime("%d.%m.%Y %H:%M"),
               booking.get_status_display()]
        if include_created:
            row.append(timezone.localtime(booking.created_at).strftime("%d.%m.%Y %H:%M"))
        writer.writerow([safe_csv_cell(value) for value in row])
    return response

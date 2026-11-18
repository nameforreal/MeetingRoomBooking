"""The administrative list and its CSV export must select the same records."""
from django.db.models import Q
from django.core.exceptions import ValidationError

from rooms.models import Booking, Room
from rooms.booking_rules import parse_calendar_date


def filtered_bookings(params):
    status = params.get("status", "")
    status = status if status in {choice[0] for choice in Booking.STATUS_CHOICES} else ""
    term = params.get("q", "").strip()[:100]
    queryset = Booking.objects.select_related("room", "user")
    room = params.get("room", "")
    if room:
        if not room.isascii() or not room.isdigit() or len(room) > 18 or not Room.objects.filter(pk=int(room)).exists():
            raise ValidationError("Выберите существующую переговорную.")
        queryset = queryset.filter(room_id=int(room))
    first = parse_calendar_date(params["date_from"]) if params.get("date_from") else None
    last = parse_calendar_date(params["date_to"]) if params.get("date_to") else None
    if first and last and first > last:
        raise ValidationError("Конец периода должен быть не раньше начала.")
    if first:
        queryset = queryset.filter(start_time__date__gte=first)
    if last:
        queryset = queryset.filter(start_time__date__lte=last)
    if status:
        queryset = queryset.filter(status=status)
    if term:
        queryset = queryset.filter(Q(room__name__icontains=term) | Q(user__username__icontains=term)
                                   | Q(user__email__icontains=term))
    return queryset, status, term

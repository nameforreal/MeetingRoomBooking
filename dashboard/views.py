from datetime import datetime, time, timedelta
from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.utils import timezone
from django.contrib import messages
from django.core.exceptions import ValidationError
from rooms import booking_rules as rules
from rooms.models import Booking, Room

@login_required
def overview(request):
    now = timezone.now()
    upcoming = Booking.objects.filter(user=request.user, end_time__gte=now).exclude(status='cancelled').select_related('room').order_by('start_time')[:6]
    today_start, tomorrow_start = rules.day_bounds(rules.site_today())
    today_count = Booking.objects.filter(user=request.user, start_time__lt=tomorrow_start, end_time__gt=today_start).exclude(status='cancelled').count()
    return render(request, 'dashboard/overview.html', {
        'today_count': today_count,
        'upcoming': upcoming,
        'rooms_count': Room.objects.filter(is_active=True).count(),
        'my_upcoming_count': Booking.objects.filter(user=request.user, end_time__gte=now).exclude(status='cancelled').count(),
        'pending_count': Booking.objects.filter(user=request.user, status='pending', end_time__gte=now).count(),
    })

@login_required
def schedule(request):
    today = rules.site_today()
    try:
        selected = rules.booking_date(request.GET.get('date'), default=today)
    except ValidationError as error:
        messages.error(request, "; ".join(error.messages))
        selected = today
    rooms = list(Room.objects.filter(is_active=True).order_by('name'))
    start, end = rules.day_bounds(selected)
    occupied = Booking.objects.filter(room__in=rooms, status__in=['pending','confirmed'], start_time__lt=end, end_time__gt=start).select_related('room')
    by_room = {}
    for item in occupied:
        by_room.setdefault(item.room_id, []).append(item)
    rows = []
    now = timezone.now()
    for room in rooms:
        cells = []
        for hour in rules.SLOT_HOURS:
            slot_start = timezone.make_aware(datetime.combine(selected, time(hour)), timezone.get_default_timezone())
            slot_end = slot_start + timedelta(hours=1)
            busy = any(b.start_time < slot_end and b.end_time > slot_start for b in by_room.get(room.pk, []))
            cells.append({'hour': hour, 'busy': busy, 'past': slot_start <= now})
        rows.append({'room': room, 'cells': cells})
    return render(request, 'dashboard/schedule.html', {'site_timezone': timezone.get_default_timezone_name(), 'rows': rows, 'hours': rules.SLOT_HOURS, 'selected': selected.isoformat(), 'today': today.isoformat(), 'max_date': rules.last_booking_date().isoformat()})

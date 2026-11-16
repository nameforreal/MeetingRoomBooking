from datetime import datetime, time, timedelta
from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.utils import timezone
from django.utils.dateparse import parse_date
from rooms.models import Booking, Room

@login_required
def overview(request):
    now = timezone.now()
    upcoming = Booking.objects.filter(user=request.user, end_time__gte=now).exclude(status='cancelled').select_related('room').order_by('start_time')[:6]
    today_start = timezone.make_aware(datetime.combine(timezone.localdate(), time.min))
    tomorrow_start = today_start + timedelta(days=1)
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
    today = timezone.localdate()
    selected = parse_date(request.GET.get('date', '')) or today
    if selected < today or selected > today + timedelta(days=60):
        selected = today
    rooms = list(Room.objects.filter(is_active=True).order_by('name'))
    start = timezone.make_aware(datetime.combine(selected, time.min))
    end = timezone.make_aware(datetime.combine(selected + timedelta(days=1), time.min))
    occupied = Booking.objects.filter(room__in=rooms, status__in=['pending','confirmed'], start_time__lt=end, end_time__gt=start).select_related('room')
    by_room = {}
    for item in occupied:
        by_room.setdefault(item.room_id, []).append(item)
    rows = []
    now = timezone.now()
    for room in rooms:
        cells = []
        for hour in range(8, 20):
            slot_start = timezone.make_aware(datetime.combine(selected, time(hour)))
            slot_end = slot_start + timedelta(hours=1)
            busy = any(b.start_time < slot_end and b.end_time > slot_start for b in by_room.get(room.pk, []))
            cells.append({'hour': hour, 'busy': busy, 'past': slot_start <= now})
        rows.append({'room': room, 'cells': cells})
    return render(request, 'dashboard/schedule.html', {'rows': rows, 'hours': range(8,20), 'selected': selected.isoformat(), 'today': today.isoformat(), 'max_date': (today+timedelta(days=60)).isoformat()})

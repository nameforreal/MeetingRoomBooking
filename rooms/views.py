from django.contrib.auth import login
from .forms import RegisterForm
from django.views.decorators.http import require_POST
from .models import Booking
from django.contrib.auth.decorators import login_required
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from datetime import timedelta, datetime, time
from django.utils.dateparse import parse_date
from django.db.models import Q

from .models import Room
from .forms import BookingForm, ProfileForm



DEMO_PHOTOS = {
    "Atlas · Conference": "space1.jpg", "Focus · Studio": "space2.jpg",
    "Horizon · Boardroom": "space3.jpg", "Nova · Workshop": "space4.jpg",
    "Lumen · Meeting": "space5.jpg", "Summit · Executive": "space6.jpg",
    "Orbit · Team": "space7.jpg", "Nexus · Strategy": "space8.jpg",
    "Pulse · Huddle": "space9.jpg",
}

def photo_for_room(room):
    if room.name in DEMO_PHOTOS:
        return DEMO_PHOTOS[room.name]
    if room.pk in (1, 2, 3):
        return f"room{room.pk}.jpg"
    return "space1.jpg"  # fallback for user-created rooms without a photo field

def home(request):
    featured = list(Room.objects.filter(is_active=True).order_by("-created_at")[:3])
    for room in featured:
        room.photo_filename = photo_for_room(room)
    return render(request, "rooms/landing.html", {
        "featured": featured, "total_rooms": Room.objects.filter(is_active=True).count()
    })

def room_detail(request, room_id):
    room = get_object_or_404(Room, pk=room_id, is_active=True)
    room.photo_filename = photo_for_room(room)
    return render(request, "rooms/room_detail.html", {"room": room})

def about(request):
    return render(request, "rooms/about.html")

def catalog(request):
    rooms = Room.objects.filter(is_active=True)
    query = request.GET.get("q", "").strip()[:80]
    if query:
        rooms = rooms.filter(Q(name__icontains=query) | Q(description__icontains=query))
    try:
        minimum_capacity = max(0, int(request.GET.get("capacity", "0")))
    except (TypeError, ValueError):
        minimum_capacity = 0
    if minimum_capacity:
        rooms = rooms.filter(capacity__gte=minimum_capacity)
    room_type = request.GET.get("type", "all")
    if room_type == "vip":
        rooms = rooms.filter(is_vip=True)
    elif room_type == "standard":
        rooms = rooms.filter(is_vip=False)
    else:
        room_type = "all"

    rooms = list(rooms)
    for room in rooms:
        room.photo_filename = photo_for_room(room)

    return render(
        request,
        "rooms/catalog.html",
        {"rooms": rooms, "minimum_capacity": minimum_capacity, "room_type": room_type, "query": query, "total_rooms": Room.objects.filter(is_active=True).count()}
    )


@login_required
def book_room(request, room_id):
    room = get_object_or_404(
        Room,
        id=room_id,
        is_active=True
    )

    if request.method == "POST":
        form = BookingForm(request.POST)

        if form.is_valid():
            booking = form.save(commit=False)
            booking.room = room
            booking.user = request.user

            try:
                with transaction.atomic():
                    # PostgreSQL: serialize requests for the same room.
                    # SQLite does not implement row-level SELECT FOR UPDATE.
                    Room.objects.select_for_update().get(pk=room.pk)
                    booking.save()
            except ValidationError as error:
                form.add_error(None, error)
            else:
                messages.success(
                    request,
                    "Бронирование успешно создано!"
                )
                return redirect("home")
    else:
        form = BookingForm()

    now = timezone.localtime()
    today = now.date()
    chosen_date = parse_date(request.GET.get("date", ""))
    if chosen_date is None or not today <= chosen_date <= today + timedelta(days=30):
        chosen_date = today
    day_start = timezone.make_aware(datetime.combine(chosen_date, time.min))
    day_end = day_start + timedelta(days=1)
    occupied = list(Booking.objects.filter(
        room=room, status__in=["pending", "confirmed"],
        end_time__gt=day_start, start_time__lt=day_end,
    ).order_by("start_time"))
    slots = []
    for hour in range(8, 20):
        start = timezone.make_aware(datetime.combine(chosen_date, time(hour, 0)))
        end = start + timedelta(hours=1)
        busy = any(item.start_time < end and item.end_time > start for item in occupied)
        slots.append({"label": f"{hour:02d}:00", "end_label": f"{hour + 1:02d}:00", "busy": busy, "past": start <= now})

    return render(
        request,
        "rooms/booking.html",
        {
            "form": form,
            "room": room,
            "occupied": occupied,
            "slots": slots,
            "chosen_date": chosen_date.isoformat(),
            "max_date": (today + timedelta(days=30)).isoformat(),
            "today": today.isoformat(),
        }
    )

@login_required
def my_bookings(request):
    bookings = Booking.objects.filter(
        user=request.user
    ).select_related("room").order_by("-start_time")

    now = timezone.now()
    all_user_bookings = Booking.objects.filter(user=request.user)
    stats = {
        "upcoming": all_user_bookings.filter(end_time__gte=now).exclude(status="cancelled").count(),
        "pending": all_user_bookings.filter(status="pending", end_time__gte=now).count(),
        "completed": all_user_bookings.filter(end_time__lt=now).exclude(status="cancelled").count(),
    }
    status_filter = request.GET.get("filter", "all")
    query = request.GET.get("q", "").strip()[:80]
    if query:
        bookings = bookings.filter(Q(room__name__icontains=query))
    if status_filter == "upcoming":
        bookings = bookings.filter(end_time__gte=now).exclude(status="cancelled")
    elif status_filter == "past":
        bookings = bookings.filter(end_time__lt=now).exclude(status="cancelled")
    elif status_filter in ("pending", "cancelled"):
        bookings = bookings.filter(status=status_filter)
    else:
        status_filter = "all"
    return render(request, "rooms/my_bookings.html", {
        "bookings": bookings, "status_filter": status_filter, "now": now, "stats": stats, "query": query,
    })


@login_required
@require_POST
def cancel_booking(request, booking_id):
    booking = get_object_or_404(
        Booking,
        id=booking_id,
        user=request.user
    )

    if booking.status != "cancelled":
        booking.status = "cancelled"
        booking.save(update_fields=["status"])
        messages.success(
            request,
            "Бронирование отменено."
        )

    return redirect("my_bookings")

def register(request):
    if request.user.is_authenticated:
        return redirect("home")

    if request.method == "POST":
        form = RegisterForm(request.POST)

        if form.is_valid():
            user = form.save()
            login(request, user)

            messages.success(
                request,
                "Регистрация прошла успешно!"
            )
            return redirect("home")
    else:
        form = RegisterForm()

    return render(
        request,
        "rooms/register.html",
        {"form": form}
    )


@login_required
def booking_detail(request, booking_id):
    booking = get_object_or_404(
        Booking.objects.select_related("room"),
        pk=booking_id,
        user=request.user,
    )
    booking.room.photo_filename = photo_for_room(booking.room)
    return render(request, "rooms/booking_detail.html", {
        "booking": booking,
        "now": timezone.now(),
    })


@login_required
def edit_booking(request, booking_id):
    booking = get_object_or_404(
        Booking.objects.select_related("room"),
        pk=booking_id,
        user=request.user,
    )
    now = timezone.now()
    if booking.status == "cancelled" or booking.end_time <= now:
        messages.error(request, "Это бронирование больше нельзя изменить.")
        return redirect("booking_detail", booking_id=booking.pk)

    if request.method == "POST":
        form = BookingForm(request.POST, instance=booking)
        if form.is_valid():
            try:
                with transaction.atomic():
                    Room.objects.select_for_update().get(pk=booking.room_id)
                    updated = form.save(commit=False)
                    updated.save()
            except ValidationError as error:
                form.add_error(None, error)
            else:
                messages.success(request, "Время встречи успешно обновлено.")
                return redirect("booking_detail", booking_id=booking.pk)
    else:
        form = BookingForm(instance=booking)

    return render(request, "rooms/edit_booking.html", {
        "form": form,
        "booking": booking,
    })


@login_required
def profile(request):
    if request.method == "POST":
        form = ProfileForm(request.POST, instance=request.user)
        if form.is_valid():
            form.save()
            messages.success(request, "Данные профиля сохранены.")
            return redirect("profile")
    else:
        form = ProfileForm(instance=request.user)

    return render(request, "rooms/profile.html", {
        "form": form,
        "booking_count": Booking.objects.filter(user=request.user).count(),
        "upcoming_count": Booking.objects.filter(
            user=request.user,
            end_time__gte=timezone.now(),
        ).exclude(status="cancelled").count(),
    })

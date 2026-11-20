from django.contrib.auth import SESSION_KEY, login, logout
from django.contrib.auth.views import redirect_to_login
from .forms import RegisterForm
from django.views.decorators.http import require_POST
from .models import Booking
from django.contrib.auth.decorators import login_required
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.utils import timezone
from datetime import timedelta, datetime, time
import re
from django.db.models import Q

from .models import Room
from . import booking_operations as operations
from . import booking_rules as rules
from .forms import CreateBookingForm, EditBookingForm, ProfileForm
from .booking_idempotency import IdempotencyError
from .audit import request_context
from .account_operations import ProfileUnavailable



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

def catalog_room_sort_key(room):
    """Order catalog rooms by their number, then list unnumbered rooms by name."""
    number = re.search(r"\d+", room.name)
    return (
        number is None,
        int(number.group()) if number else 0,
        room.name.casefold(),
        room.pk,
    )

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

    rooms = sorted(rooms, key=catalog_room_sort_key)
    for room in rooms:
        room.photo_filename = photo_for_room(room)

    return render(
        request,
        "rooms/catalog.html",
        {"rooms": rooms, "minimum_capacity": minimum_capacity, "room_type": room_type, "query": query, "total_rooms": Room.objects.filter(is_active=True).count()}
    )


@login_required
@request_context("html")
def book_room(request, room_id):
    response_status = 200
    if request.method == "POST":
        form = CreateBookingForm(request.POST)

        if form.is_valid():
            try:
                operations.create_once(actor=request.user, room_id=room_id,
                                  key=form.cleaned_data["idempotency_key"],
                                  start_time=form.cleaned_data["start_time"],
                                  end_time=form.cleaned_data["end_time"])
            except ValidationError as error:
                form.add_error(None, error)
                if isinstance(error, IdempotencyError):
                    response_status = error.status
            else:
                messages.success(
                    request,
                    "Бронирование успешно создано!"
                )
                return redirect("home")
    else:
        form = CreateBookingForm()

    # A successful POST replay above also works after the room is archived.
    room = get_object_or_404(Room, id=room_id, **({"is_active": True} if request.method != "POST" else {}))

    now = timezone.localtime(timezone.now(), timezone.get_default_timezone())
    today = rules.site_today()
    try:
        chosen_date = rules.booking_date(request.GET.get("date"), default=today)
    except ValidationError as error:
        messages.error(request, "; ".join(error.messages))
        chosen_date = today
    day_start, day_end = rules.day_bounds(chosen_date)
    occupied = list(Booking.objects.filter(
        room=room, status__in=["pending", "confirmed"],
        end_time__gt=day_start, start_time__lt=day_end,
    ).order_by("start_time"))
    slots = []
    for hour in rules.SLOT_HOURS:
        start = timezone.make_aware(datetime.combine(chosen_date, time(hour, 0)), timezone.get_default_timezone())
        end = start + timedelta(hours=1)
        busy = any(item.start_time < end and item.end_time > start for item in occupied)
        slots.append({"label": f"{hour:02d}:00", "end_label": f"{hour + 1:02d}:00", "busy": busy, "past": start <= now})

    response = render(
        request,
        "rooms/booking.html",
        {
            "form": form,
            "room": room,
            "occupied": occupied,
            "slots": slots,
            "chosen_date": chosen_date.isoformat(),
            "max_date": rules.last_booking_date().isoformat(),
            "today": today.isoformat(),
            "site_timezone": timezone.get_default_timezone_name(),
        }, status=response_status
    )
    if response_status == 503:
        response["Retry-After"] = "1"
    return response

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
@request_context("html")
def cancel_booking(request, booking_id):
    booking = get_object_or_404(
        Booking,
        id=booking_id,
        user=request.user
    )

    try:
        operations.cancel(booking, actor=request.user, revision=request.POST.get("revision"))
    except ValidationError as error:
        messages.error(request, "; ".join(error.messages))
    else:
        messages.success(request, "Бронирование отменено.")

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
@request_context("html")
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
        form = EditBookingForm(request.POST, instance=booking)
        if form.is_valid():
            try:
                updated = operations.edit(
                    booking, actor=request.user,
                    revision=form.cleaned_data["revision"],
                    room=form.cleaned_data["room"],
                    start_time=form.cleaned_data["start_time"],
                    end_time=form.cleaned_data["end_time"],
                )
            except ValidationError as error:
                form.add_error(None, error)
            else:
                messages.success(request, "Бронирование обновлено." + (
                    " VIP-заявка ожидает подтверждения." if updated.status == "pending" else ""
                ))
                return redirect("booking_detail", booking_id=booking.pk)
    else:
        form = EditBookingForm(instance=booking)

    return render(request, "rooms/edit_booking.html", {
        "form": form,
        "booking": booking,
        "site_timezone": timezone.get_default_timezone_name(),
    })


def _end_profile_session(request):
    logout(request)
    messages.error(request, "Учётная запись недоступна или сеанс недействителен. Войдите снова; если доступ отключён, обратитесь к администратору.")
    return redirect_to_login(request.get_full_path())


def profile(request):
    if not request.user.is_authenticated:
        if SESSION_KEY in request.session:
            return _end_profile_session(request)
        return redirect_to_login(request.get_full_path())
    if request.method == "POST":
        form = ProfileForm(request.POST, instance=request.user)
        if form.is_valid():
            try:
                form.save()
            except ProfileUnavailable:
                return _end_profile_session(request)
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

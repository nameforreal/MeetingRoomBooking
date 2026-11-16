from django.contrib.auth import login
from .forms import RegisterForm
from django.views.decorators.http import require_POST
from .models import Booking
from django.contrib.auth.decorators import login_required
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction

from .models import Room
from .forms import BookingForm


def home(request):
    rooms = Room.objects.filter(is_active=True)

    return render(
        request,
        "rooms/home.html",
        {"rooms": rooms}
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

    return render(
        request,
        "rooms/booking.html",
        {
            "form": form,
            "room": room,
        }
    )

@login_required
def my_bookings(request):
    bookings = Booking.objects.filter(
        user=request.user
    ).select_related("room").order_by("-start_time")

    return render(
        request,
        "rooms/my_bookings.html",
        {"bookings": bookings}
    )


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

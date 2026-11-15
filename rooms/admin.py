from django.contrib import admin
from .models import Room, Booking


@admin.register(Room)
class RoomAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "capacity",
        "is_vip",
        "is_active",
        "created_at",
    )

    list_filter = ("is_vip", "is_active")
    search_fields = ("name", "description")


@admin.register(Booking)
class BookingAdmin(admin.ModelAdmin):
    list_display = (
        "room",
        "user",
        "start_time",
        "end_time",
        "status",
        "created_at",
    )

    list_filter = ("status", "room")
    search_fields = ("room__name", "user__username")
    readonly_fields = ("created_at",)

    actions = [
        "confirm_bookings",
        "reject_bookings",
    ]

    @admin.action(description="Подтвердить выбранные бронирования")
    def confirm_bookings(self, request, queryset):
        confirmed = 0

        for booking in queryset.filter(status="pending"):
            booking.status = "confirmed"
            booking.save(update_fields=["status"])
            confirmed += 1

        self.message_user(
            request,
            f"Подтверждено бронирований: {confirmed}"
        )

    @admin.action(description="Отклонить выбранные бронирования")
    def reject_bookings(self, request, queryset):
        rejected = queryset.filter(
            status="pending"
        ).update(status="cancelled")

        self.message_user(
            request,
            f"Отклонено бронирований: {rejected}"
        )

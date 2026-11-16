import csv
from django.contrib import admin, messages
from django.http import HttpResponse
from django.utils import timezone
from django.core.exceptions import ValidationError
from .models import Room, Booking


@admin.register(Room)
class RoomAdmin(admin.ModelAdmin):
    list_display = ("name", "capacity", "room_type", "is_active", "created_at")
    list_filter = ("is_vip", "is_active", "created_at")
    search_fields = ("name", "description")
    ordering = ("name",)
    readonly_fields = ("created_at",)
    list_per_page = 25
    fieldsets = (
        ("Основная информация", {"fields": ("name", "description", "capacity")}),
        ("Параметры бронирования", {"fields": ("is_vip", "is_active")}),
        ("Служебная информация", {"fields": ("created_at",)}),
    )

    @admin.display(description="Категория", ordering="is_vip")
    def room_type(self, obj):
        return "VIP" if obj.is_vip else "Стандарт"


@admin.register(Booking)
class BookingAdmin(admin.ModelAdmin):
    list_display = ("room", "user", "start_time", "end_time", "status", "created_at")
    list_filter = ("status", "room", ("start_time", admin.DateFieldListFilter))
    search_fields = ("room__name", "user__username", "user__email")
    date_hierarchy = "start_time"
    readonly_fields = ("created_at",)
    ordering = ("-start_time",)
    list_per_page = 50
    actions = ["confirm_bookings", "reject_bookings", "export_bookings_csv"]
    fieldsets = (
        ("Бронирование", {"fields": ("room", "user", "start_time", "end_time", "status")}),
        ("Служебная информация", {"fields": ("created_at",)}),
    )

    @admin.action(description="Подтвердить выбранные бронирования")
    def confirm_bookings(self, request, queryset):
        confirmed, failed = 0, 0
        for booking in queryset.filter(status="pending").select_related("room"):
            booking.status = "confirmed"
            try:
                booking.save(update_fields=["status"])
                confirmed += 1
            except ValidationError:
                failed += 1
        if confirmed:
            self.message_user(request, f"Подтверждено бронирований: {confirmed}.", messages.SUCCESS)
        if failed:
            self.message_user(request, f"Не удалось подтвердить из-за ошибок проверки: {failed}.", messages.ERROR)

    @admin.action(description="Отклонить выбранные ожидающие бронирования")
    def reject_bookings(self, request, queryset):
        updated = queryset.filter(status="pending").update(status="cancelled")
        self.message_user(request, f"Отклонено бронирований: {updated}.", messages.SUCCESS)

    @admin.action(description="Экспортировать выбранные бронирования в CSV")
    def export_bookings_csv(self, request, queryset):
        response = HttpResponse(content_type="text/csv; charset=utf-8-sig")
        response["Content-Disposition"] = 'attachment; filename="meetingroom-bookings.csv"'
        writer = csv.writer(response, delimiter=";")
        writer.writerow(["ID", "Переговорная", "Пользователь", "Email", "Начало", "Окончание", "Статус", "Создано"])
        for booking in queryset.select_related("room", "user").order_by("start_time"):
            writer.writerow([
                booking.pk, booking.room.name, booking.user.username, booking.user.email,
                timezone.localtime(booking.start_time).strftime("%d.%m.%Y %H:%M"),
                timezone.localtime(booking.end_time).strftime("%d.%m.%Y %H:%M"),
                booking.get_status_display(),
                timezone.localtime(booking.created_at).strftime("%d.%m.%Y %H:%M"),
            ])
        return response

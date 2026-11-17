import csv
from django.contrib import admin, messages
from django.http import HttpResponse
from django.utils import timezone
from django.core.exceptions import ValidationError
from .models import Room, Booking
from . import booking_operations as operations
from .forms import RevisionField
from django.http import HttpResponseRedirect
from django.urls import reverse
from django import forms
from .permissions import staff_has_perm, can_edit_account
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin
from django.core.exceptions import PermissionDenied


@admin.register(Room)
class RoomAdmin(admin.ModelAdmin):
    def changeform_view(self, request, object_id=None, form_url="", extra_context=None):
        try:
            return super().changeform_view(request, object_id, form_url, extra_context)
        except ValidationError as error:
            self.message_user(request, "; ".join(error.messages), messages.ERROR)
            return HttpResponseRedirect(request.path)

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


class BookingAdminForm(forms.ModelForm):
    expected_revision = RevisionField(required=False, widget=forms.HiddenInput)

    class Meta:
        model = Booking
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.initial["expected_revision"] = self.instance.revision
        self.fields["expected_revision"].required = not self.instance._state.adding


@admin.register(Booking)
class BookingAdmin(admin.ModelAdmin):
    form = BookingAdminForm
    change_list_template = "admin/rooms/booking/change_list.html"
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
        ("Служебная информация", {"fields": ("created_at", "expected_revision")}),
    )

    def save_model(self, request, obj, form, change):
        # Django has checked add/change permissions before this hook.
        revision = form.cleaned_data.get("expected_revision")
        if change and obj.status == "cancelled":
            if {"room", "user", "start_time", "end_time"}.intersection(form.changed_data):
                raise ValidationError("Отмена не должна одновременно изменять параметры встречи.")
            result = operations.cancel(obj, actor=request.user, revision=revision)
            obj._booking_noop = getattr(result, "_booking_noop", False)
        else:
            operations.persist(obj, actor=request.user, revision=revision)

    def log_change(self, request, obj, message):
        if not getattr(obj, "_booking_noop", False):
            return super().log_change(request, obj, message)

    def changeform_view(self, request, object_id=None, form_url="", extra_context=None):
        try:
            return super().changeform_view(request, object_id, form_url, extra_context)
        except ValidationError as error:
            # A race discovered after form validation must roll back, not return 500.
            self.message_user(request, "; ".join(error.messages), messages.ERROR)
            return HttpResponseRedirect(request.path)

    @admin.action(permissions=["change"], description="Подтвердить выбранные бронирования")
    def confirm_bookings(self, request, queryset):
        if not staff_has_perm(request.user, "rooms.change_booking"):
            raise PermissionDenied
        if not self._reviewed_selection(request):
            return
        confirmed, failed = 0, 0
        for booking in queryset.select_related("room"):
            try:
                operations.confirm(booking, actor=request.user,
                                   revision=request.POST.get(f"revision_{booking.pk}"))
                confirmed += 1
            except ValidationError:
                failed += 1
        if confirmed:
            self.message_user(request, f"Подтверждено бронирований: {confirmed}.", messages.SUCCESS)
        if failed:
            self.message_user(request, f"Не удалось подтвердить из-за ошибок проверки: {failed}.", messages.ERROR)

    @admin.action(permissions=["change"], description="Отклонить выбранные ожидающие бронирования")
    def reject_bookings(self, request, queryset):
        if not staff_has_perm(request.user, "rooms.change_booking"):
            raise PermissionDenied
        if not self._reviewed_selection(request):
            return
        updated = 0
        for booking in queryset.order_by("pk"):
            try:
                operations.reject(booking, actor=request.user,
                                  revision=request.POST.get(f"revision_{booking.pk}"))
                updated += 1
            except ValidationError as error:
                self.message_user(request, "; ".join(error.messages), messages.ERROR)
        self.message_user(request, f"Отклонено бронирований: {updated}.", messages.SUCCESS)

    def _reviewed_selection(self, request):
        if request.POST.get("select_across", "0") != "0":
            self.message_user(request, "Выберите заявки на просмотренной странице: согласование всех результатов поиска запрещено.", messages.ERROR)
            return False
        return True

    def has_export_permission(self, request):
        return staff_has_perm(request.user, "rooms.export_booking")

    @admin.action(permissions=["export"], description="Экспортировать выбранные бронирования в CSV")
    def export_bookings_csv(self, request, queryset):
        if not staff_has_perm(request.user, "rooms.export_booking"):
            raise PermissionDenied
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


# Standard UserAdmin exposes groups and user_permissions to change_user holders.
# Keep account management limited in both administrative interfaces.
admin.site.unregister(get_user_model())


@admin.register(get_user_model())
class RestrictedUserAdmin(UserAdmin):
    def has_add_permission(self, request):
        return request.user.is_superuser and super().has_add_permission(request)

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser and super().has_delete_permission(request, obj)

    def has_change_permission(self, request, obj=None):
        if obj is None:
            return staff_has_perm(request.user, "auth.change_user")
        return can_edit_account(request.user, obj)

    def get_fieldsets(self, request, obj=None):
        if request.user.is_superuser:
            return super().get_fieldsets(request, obj)
        return ((None, {"fields": ("username", "first_name", "last_name", "email", "is_active")}),)

    def user_change_password(self, request, id, form_url=""):
        if not request.user.is_superuser:
            raise PermissionDenied
        return super().user_change_password(request, id, form_url)

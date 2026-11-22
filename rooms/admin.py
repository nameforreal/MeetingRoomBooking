from django.contrib import admin, messages
from django.core.exceptions import ValidationError
from .models import Room, Booking, AuditEvent, Role
from . import audit, account_operations
from .csv_export import export_bookings
from urllib.parse import urlencode
from . import booking_operations as operations
from .forms import RevisionField, IdempotencyKeyField
from .booking_idempotency import IdempotencyError
from django.forms.models import construct_instance
from django.http import HttpResponseRedirect
from django.urls import reverse
from django import forms
from .permissions import staff_has_perm, can_edit_account
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin
from django.contrib.auth.forms import AdminPasswordChangeForm
from django.db import router, transaction
from django.core.exceptions import PermissionDenied


class HistoryAdminMixin:
    def has_delete_permission(self, request, obj=None):
        return False

    def delete_model(self, request, obj):
        raise PermissionDenied

    def delete_queryset(self, request, queryset):
        raise PermissionDenied

    def changeform_view(self, request, *args, **kwargs):
        with audit.context(source="django_admin", actor=request.user):
            return super().changeform_view(request, *args, **kwargs)

    def changelist_view(self, request, *args, **kwargs):
        with audit.context(source="django_admin", actor=request.user):
            return super().changelist_view(request, *args, **kwargs)

    def history_view(self, request, object_id, extra_context=None):
        if not staff_has_perm(request.user, "rooms.view_auditevent"):
            raise PermissionDenied
        query = urlencode({"object_type__exact": self.model._meta.model_name, "object_id__exact": object_id})
        return HttpResponseRedirect(reverse("admin:rooms_auditevent_changelist") + "?" + query)


@admin.register(Role)
class RoleAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "group", "updated_at")
    list_select_related = ("group",)
    search_fields = ("group__name", "code", "description")
    ordering = ("group__name",)
    readonly_fields = ("group", "code", "created_at", "updated_at")
    fields = ("group", "code", "description", "created_at", "updated_at")

    def has_add_permission(self, request):
        # Roles are provisioned by the idempotent setup_roles command.
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Room)
class RoomAdmin(HistoryAdminMixin, admin.ModelAdmin):
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
    idempotency_key = IdempotencyKeyField(required=False)

    class Meta:
        model = Booking
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.initial["expected_revision"] = self.instance.revision
        self.fields["expected_revision"].required = not self.instance._state.adding
        self.fields["idempotency_key"].required = self.instance._state.adding

    def _post_clean(self):
        if self.instance._state.adding:
            # Fields have been parsed, but model.clean() would reject a replay
            # after cancellation/archival/time passing. The operation validates
            # new bookings under its room lock instead.
            self.instance = construct_instance(self, self.instance, self._meta.fields, self._meta.exclude)
        else:
            super()._post_clean()


@admin.register(Booking)
class BookingAdmin(HistoryAdminMixin, admin.ModelAdmin):
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
        ("Служебная информация", {"fields": ("created_at", "expected_revision", "idempotency_key")}),
    )

    def get_form(self, request, obj=None, **kwargs):
        base = super().get_form(request, obj, **kwargs)
        class RequestForm(base):
            def clean(self):
                data = super().clean()
                if hasattr(request, "_booking_creation_error"):
                    raise request._booking_creation_error
                return data
        return RequestForm

    def save_model(self, request, obj, form, change):
        # Django has checked add/change permissions before this hook.
        if not change:
            result = operations.create_once(actor=request.user, key=form.cleaned_data["idempotency_key"],
                                            room_id=obj.room_id, user=obj.user,
                                            start_time=obj.start_time, end_time=obj.end_time)
            obj.__dict__.update(result.booking.__dict__)
            obj._booking_noop = result.replayed
            return
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

    def log_addition(self, request, obj, message):
        if not getattr(obj, "_booking_noop", False):
            return super().log_addition(request, obj, message)

    def changeform_view(self, request, object_id=None, form_url="", extra_context=None):
        try:
            return super().changeform_view(request, object_id, form_url, extra_context)
        except ValidationError as error:
            if object_id is None:
                # Re-render the bound form with its original key, without
                # rerunning the failed operation or producing a fresh key.
                request._booking_creation_error = error
                response = super().changeform_view(request, object_id, form_url, extra_context)
                if isinstance(error, IdempotencyError):
                    response.status_code = error.status
                    if error.status == 503:
                        response["Retry-After"] = "1"
                return response
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
        if request.POST.get("select_across", "0") != "0":
            self.message_user(request, "Для экспорта выберите конкретные записи. Экспорт всех результатов поиска этим действием запрещён.", messages.ERROR)
            return
        return export_bookings(queryset, include_created=True)


# Standard UserAdmin exposes groups and user_permissions to change_user holders.
# Keep account management limited in both administrative interfaces.
admin.site.unregister(get_user_model())


class ControlledAdminPasswordChangeForm(AdminPasswordChangeForm):
    def save(self, commit=True):
        # Keep Django's hashing/unusable-password logic, never its full User.save().
        user = super().save(commit=False)
        if commit:
            _, actor, _ = audit.identity()
            if actor is None:
                raise PermissionDenied
            self.user = account_operations.save_account(user, actor=actor, fields=["password"])
        return self.user


@admin.register(get_user_model())
class RestrictedUserAdmin(HistoryAdminMixin, UserAdmin):
    change_password_form = ControlledAdminPasswordChangeForm

    def get_form(self, request, obj=None, **kwargs):
        base = super().get_form(request, obj, **kwargs)

        class AccountForm(base):
            def clean(self):
                data = super().clean()
                if not self.instance._state.adding:
                    account_operations.validate_account_policy(
                        self.instance, actor=request.user,
                        values={key: data[key] for key in ("is_active", "is_staff") if key in data},
                    )
                return data

        return AccountForm

    def changeform_view(self, request, *args, **kwargs):
        try:
            return super().changeform_view(request, *args, **kwargs)
        except ValidationError as error:
            self.message_user(request, "; ".join(error.messages), messages.ERROR)
            return HttpResponseRedirect(request.path)

    def has_add_permission(self, request):
        return request.user.is_superuser and super().has_add_permission(request)

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        fields = {field.name for field in obj._meta.concrete_fields if not field.primary_key}
        account_operations.save_account(obj, actor=request.user,
                                        fields=fields.intersection(form.changed_data) if change else None)

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
        try:
            with audit.context(source="django_admin", actor=request.user):
                # Include the standard LogEntry in the password transaction.
                with transaction.atomic(using=router.db_for_write(self.model)):
                    return super().user_change_password(request, id, form_url)
        except ValidationError as error:
            self.message_user(request, "; ".join(error.messages), messages.ERROR)
            return HttpResponseRedirect(request.path)


@admin.register(AuditEvent)
class AuditEventAdmin(admin.ModelAdmin):
    list_display = ("occurred_at", "action", "object_type", "object_id", "actor", "source", "revision_after", "correlation_id")
    list_filter = ("source", "action", "object_type", "occurred_at")
    search_fields = ("=object_id", "=actor__id", "=correlation_id")
    readonly_fields = tuple(field.name for field in AuditEvent._meta.fields)
    list_per_page = 50
    actions = None

    def has_view_permission(self, request, obj=None):
        return staff_has_perm(request.user, "rooms.view_auditevent")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def delete_model(self, request, obj):
        raise PermissionDenied

    def delete_queryset(self, request, queryset):
        raise PermissionDenied

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("actor", "booking", "room", "subject_user",
                                                           "previous_room", "new_room", "previous_owner", "new_owner")

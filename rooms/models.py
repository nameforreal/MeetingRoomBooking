from django.db import models
from .querysets import RoomQuerySet
from django.db.models.deletion import ProtectedError
from uuid import uuid4


class Room(models.Model):
    objects = RoomQuerySet.as_manager()

    def clean(self):
        from .room_rules import validate_capacity
        super().clean()
        try:
            validate_capacity(self.capacity)
        except ValidationError as error:
            raise ValidationError({"capacity": error.messages}) from error

    def delete(self, *args, **kwargs):
        raise ProtectedError("Use the controlled deletion procedure for rooms without history.", [self])

    @classmethod
    def from_db(cls, db, field_names, values):
        instance = super().from_db(db, field_names, values)
        instance._room_flags = {name: value for name, value in zip(field_names, values)
                                if name in ("is_vip", "is_active")}
        return instance

    def refresh_from_db(self, *args, **kwargs):
        super().refresh_from_db(*args, **kwargs)
        self._room_flags = {name: getattr(self, name) for name in ("is_vip", "is_active")
                            if name not in self.get_deferred_fields()}

    def save(self, *, force_insert=False, force_update=False, using=None, update_fields=None):
        from .booking_operations import save_room
        save_room(self, force_insert=force_insert, force_update=force_update,
                  using=using, update_fields=update_fields)

    name = models.CharField(
        max_length=100,
        verbose_name="Название комнаты"
    )

    description = models.TextField(
        blank=True,
        verbose_name="Описание"
    )

    capacity = models.PositiveIntegerField(
        default=1,
        verbose_name="Вместимость"
    )

    is_vip = models.BooleanField(
        default=False,
        verbose_name="VIP-комната"
    )

    is_active = models.BooleanField(
        default=True,
        verbose_name="Доступна для бронирования"
    )

    photo = models.ImageField(upload_to="room_photos/", blank=True, null=True, verbose_name="Фотография")

    created_at = models.DateTimeField(
        auto_now_add=True,
        verbose_name="Дата создания"
    )

    def __str__(self):
        return self.name

    class Meta:
        base_manager_name = "objects"
        default_manager_name = "objects"
        verbose_name = "Переговорная комната"
        verbose_name_plural = "Переговорные комнаты"
        ordering = ["name"]


# Create your models here.

from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils import timezone


from .querysets import BookingQuerySet


class CreationRequestQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise TypeError("Creation receipts are immutable; use booking_operations.create_once().")

    def delete(self):
        raise TypeError("Creation receipts have no TTL and must not be deleted.")

    def bulk_create(self, objs, **kwargs):
        raise TypeError("Use booking_operations.create_once().")

    def bulk_update(self, objs, fields, **kwargs):
        raise TypeError("Creation receipts are immutable.")

    def update_or_create(self, *args, **kwargs):
        raise TypeError("Creation receipts are immutable.")


class BookingCreationRequest(models.Model):
    """A receipt committed with its booking; reservations never commit alone."""

    objects = CreationRequestQuerySet.as_manager()
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    operation = models.CharField(max_length=32, default="booking.create", editable=False)
    key = models.CharField(max_length=128)
    normalization_version = models.PositiveSmallIntegerField(default=1)
    canonical_payload = models.JSONField()
    payload_hash = models.CharField(max_length=64)
    booking = models.ForeignKey("Booking", null=True, on_delete=models.PROTECT)
    response_payload = models.JSONField(null=True)
    response_status = models.PositiveSmallIntegerField(default=201)
    response_version = models.PositiveSmallIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True)

    def save(self, *args, **kwargs):
        raise TypeError("Use booking_operations.create_once(); receipts must commit with bookings.")

    def delete(self, *args, **kwargs):
        raise TypeError("Creation receipts have no TTL and must not be deleted.")

    class Meta:
        base_manager_name = "objects"
        default_manager_name = "objects"
        constraints = [
            models.UniqueConstraint(fields=["actor", "operation", "key"], name="booking_creation_key_unique"),
            models.CheckConstraint(
                condition=(models.Q(booking__isnull=True, response_payload__isnull=True, completed_at__isnull=True)
                           | models.Q(booking__isnull=False, response_payload__isnull=False, completed_at__isnull=False)),
                name="booking_creation_result_consistent",
            ),
        ]


class Booking(models.Model):
    objects = BookingQuerySet.as_manager()
    revision = models.PositiveIntegerField(default=0, editable=False)

    @classmethod
    def from_db(cls, db, field_names, values):
        instance = super().from_db(db, field_names, values)
        from .booking_operations import FIELDS, snapshot
        if set(FIELDS).issubset(field_names):
            instance._booking_snapshot = snapshot(instance)
        return instance

    def refresh_from_db(self, *args, **kwargs):
        super().refresh_from_db(*args, **kwargs)
        from .booking_operations import FIELDS, snapshot
        if not set(FIELDS).intersection(self.get_deferred_fields()):
            self._booking_snapshot = snapshot(self)

    STATUS_CHOICES = [
        ("pending", "Ожидает подтверждения"),
        ("confirmed", "Подтверждено"),
        ("cancelled", "Отменено"),
    ]

    room = models.ForeignKey(
        Room,
        on_delete=models.PROTECT,
        related_name="bookings",
        verbose_name="Переговорная комната"
    )

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="bookings",
        verbose_name="Пользователь"
    )

    start_time = models.DateTimeField(
        verbose_name="Начало бронирования"
    )

    end_time = models.DateTimeField(
        verbose_name="Конец бронирования"
    )

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default="confirmed",
        verbose_name="Статус"
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
        verbose_name="Дата создания"
    )

    def clean(self):
        from .booking_rules import validate_interval
        previous = self._apply_status_policy()
        if not self.start_time or not self.end_time:
            return
        rescheduled = previous is None or any(
            getattr(self, field) != getattr(previous, field)
            for field in ("room_id", "start_time", "end_time")
        )
        validate_interval(self.start_time, self.end_time, scheduling=rescheduled)

        if self.room_id and self.status != "cancelled":
            conflicts = Booking.objects.filter(
                room_id=self.room_id,
                start_time__lt=self.end_time,
                end_time__gt=self.start_time,
                status__in=["pending", "confirmed"],
            ).exclude(pk=self.pk)

            if conflicts.exists():
                raise ValidationError(
                    "Комната уже забронирована на это время."
                )

    def _apply_status_policy(self):
        """Approval belongs to a particular room and time interval.

        Explicit confirmation of an unchanged pending request is authorized by
        the administrative endpoints. Public forms never accept a status.
        """
        previous = None
        if self.pk:
            previous = type(self).objects.filter(pk=self.pk).first()
        if previous is None:
            if self.room_id:
                self.status = "pending" if self.room.is_vip else "confirmed"
            return

        changed = any(
            getattr(self, field) != getattr(previous, field)
            for field in ("room_id", "start_time", "end_time")
        )
        if previous.status == "cancelled":
            if self.status != "cancelled" or changed:
                raise ValidationError("Отменённое бронирование нельзя изменять или подтверждать.")
            return previous
        if self.status == "cancelled":
            return previous
        leaving_approved_vip = (
            previous.status == "confirmed" and previous.room_id != self.room_id and previous.room.is_vip
        )
        if changed and self.room_id and (self.room.is_vip or leaving_approved_vip):
            self.status = "pending"
        elif self.status != previous.status and (previous.status, self.status) != ("pending", "confirmed"):
            raise ValidationError("Недопустимое изменение статуса бронирования.")

        return previous

    def save(self, *, force_insert=False, force_update=False, using=None, update_fields=None, revision=None):
        from .booking_operations import persist
        persist(self, force_insert=force_insert, force_update=force_update,
                using=using, update_fields=update_fields, revision=revision)

    def __str__(self):
        return f"{self.room} — {self.start_time}"

    def delete(self, *args, **kwargs):
        raise ProtectedError("Booking history must not be deleted.", [self])

    class Meta:
        base_manager_name = "objects"
        default_manager_name = "objects"
        permissions = [("export_booking", "Can export bookings")]
        verbose_name = "Бронирование"
        verbose_name_plural = "Бронирования"
        ordering = ["-start_time"]


class AuditEventQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise TypeError("Audit events are immutable.")

    def delete(self):
        raise ProtectedError("Audit history must not be deleted.", [])

    def bulk_create(self, objs, **kwargs):
        raise TypeError("Audit events must be appended by the operation recorder.")

    def bulk_update(self, objs, fields, **kwargs):
        raise TypeError("Audit events are immutable.")

    def update_or_create(self, *args, **kwargs):
        raise TypeError("Audit events are immutable.")


class AuditEvent(models.Model):
    """Append-only business history. Insert only inside the owning operation."""
    objects = AuditEventQuerySet.as_manager()
    occurred_at = models.DateTimeField(default=timezone.now, editable=False)
    action = models.CharField(max_length=40)
    source = models.CharField(max_length=20, choices=[(x, x) for x in
                               ("html", "api", "django_admin", "management", "system")])
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="+")
    object_type = models.CharField(max_length=12, choices=[(x, x) for x in ("booking", "room", "user")])
    object_id = models.PositiveBigIntegerField()
    booking = models.ForeignKey(Booking, null=True, on_delete=models.PROTECT, related_name="audit_events")
    room = models.ForeignKey(Room, null=True, on_delete=models.PROTECT, related_name="audit_events")
    subject_user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="+")
    previous_room = models.ForeignKey(Room, null=True, on_delete=models.PROTECT, related_name="+")
    new_room = models.ForeignKey(Room, null=True, on_delete=models.PROTECT, related_name="+")
    previous_owner = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="+")
    new_owner = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="+")
    before = models.JSONField(default=dict)
    after = models.JSONField(default=dict)
    changed_fields = models.JSONField(default=list)
    revision_before = models.PositiveIntegerField(null=True)
    revision_after = models.PositiveIntegerField(null=True)
    correlation_id = models.UUIDField(default=uuid4, editable=False)
    schema_version = models.PositiveSmallIntegerField(default=1)

    def save(self, *args, **kwargs):
        raise TypeError("Audit events may only be appended by the operation recorder.")

    def delete(self, *args, **kwargs):
        raise ProtectedError("Audit history must not be deleted.", [self])

    class Meta:
        base_manager_name = "objects"
        default_manager_name = "objects"
        default_permissions = ("view",)
        ordering = ("-occurred_at", "-id")
        indexes = [
            models.Index(fields=["occurred_at", "id"], name="audit_time_idx"),
            models.Index(fields=["actor", "occurred_at"], name="audit_actor_time_idx"),
            models.Index(fields=["action", "occurred_at"], name="audit_action_time_idx"),
            models.Index(fields=["correlation_id", "id"], name="audit_correlation_idx"),
        ]
        constraints = [
            models.UniqueConstraint(fields=["booking", "revision_after"],
                                    condition=models.Q(booking__isnull=False) & ~models.Q(action="baseline"),
                                    name="audit_booking_revision_unique"),
            models.UniqueConstraint(fields=["booking"], condition=models.Q(action="baseline"),
                                    name="audit_booking_baseline_unique"),
            models.CheckConstraint(condition=models.Q(source="system") | models.Q(actor__isnull=False),
                                   name="audit_actor_required"),
            models.CheckConstraint(condition=models.Q(source__in=["html", "api", "django_admin", "management", "system"]),
                                   name="audit_source_valid"),
        ]

class Role(models.Model):
    """Persistent role metadata linked to Django's permission-bearing Group."""

    group = models.OneToOneField(
        "auth.Group",
        on_delete=models.PROTECT,
        related_name="meetingroom_role",
        verbose_name="Группа разрешений",
    )
    code = models.SlugField(max_length=100, unique=True, verbose_name="Код роли")
    description = models.TextField(blank=True, verbose_name="Описание")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Дата создания")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Дата изменения")

    @property
    def name(self):
        return self.group.name

    def __str__(self):
        return self.name

    class Meta:
        verbose_name = "Роль"
        verbose_name_plural = "Роли"
        ordering = ("group__name",)

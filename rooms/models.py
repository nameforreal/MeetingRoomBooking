from django.db import models
from .querysets import RoomQuerySet


class Room(models.Model):
    objects = RoomQuerySet.as_manager()

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
        verbose_name = "Переговорная комната"
        verbose_name_plural = "Переговорные комнаты"
        ordering = ["name"]


# Create your models here.

from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils import timezone


from .querysets import BookingQuerySet


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
        on_delete=models.CASCADE,
        related_name="bookings",
        verbose_name="Переговорная комната"
    )

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
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
        if changed and self.room_id and self.room.is_vip:
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

    class Meta:
        base_manager_name = "objects"
        default_manager_name = "objects"
        permissions = [("export_booking", "Can export bookings")]
        verbose_name = "Бронирование"
        verbose_name_plural = "Бронирования"
        ordering = ["-start_time"]

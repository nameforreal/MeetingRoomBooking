from django.db import models


class Room(models.Model):
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


class Booking(models.Model):
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
        if not self.start_time or not self.end_time:
            return

        if self.start_time >= self.end_time:
            raise ValidationError(
                "Время окончания должно быть позже начала."
            )

        duration = self.end_time - self.start_time

        if not (
            timezone.timedelta(hours=1)
            <= duration
            <= timezone.timedelta(hours=4)
        ):
            raise ValidationError(
                "Бронирование должно длиться от 1 до 4 часов."
            )

        if self.status != "cancelled" and self.start_time < timezone.now():
            raise ValidationError(
                "Нельзя бронировать время в прошлом."
            )

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

    def save(self, *args, **kwargs):
        if self._state.adding and self.room_id:
            self.status = (
                "pending" if self.room.is_vip else "confirmed"
            )

        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.room} — {self.start_time}"

    class Meta:
        verbose_name = "Бронирование"
        verbose_name_plural = "Бронирования"
        ordering = ["-start_time"]

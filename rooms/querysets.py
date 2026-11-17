from django.db import models
from django.db.models.deletion import ProtectedError


class RoomQuerySet(models.QuerySet):
    def delete(self):
        raise ProtectedError("Use the controlled deletion procedure for rooms without history.", [])

    def bulk_create(self, objs, batch_size=None, ignore_conflicts=False,
                    update_conflicts=False, update_fields=None, unique_fields=None):
        if update_conflicts and {"is_vip", "is_active"}.intersection(update_fields or ()):
            raise TypeError("Room flags must be changed via Room.save().")
        return super().bulk_create(
            objs, batch_size=batch_size, ignore_conflicts=ignore_conflicts,
            update_conflicts=update_conflicts, update_fields=update_fields,
            unique_fields=unique_fields,
        )

    def update(self, **kwargs):
        if {"is_vip", "is_active"}.intersection(kwargs):
            raise TypeError("Room flags must be changed via Room.save().")
        return super().update(**kwargs)

    def bulk_update(self, objs, fields, **kwargs):
        if {"is_vip", "is_active"}.intersection(fields):
            raise TypeError("Room flags must be changed via Room.save().")
        return super().bulk_update(objs, fields, **kwargs)


class BookingQuerySet(models.QuerySet):
    def delete(self):
        raise ProtectedError("Booking history must not be deleted.", [])

    def update_or_create(self, *args, **kwargs):
        # Django locks the booking first, which would invert our lock order.
        raise TypeError("Booking.update_or_create() is disabled; use rooms.booking_operations.")

    def update(self, **kwargs):
        raise TypeError("Booking.update() is disabled; use rooms.booking_operations.")

    def bulk_create(self, objs, **kwargs):
        raise TypeError("Booking.bulk_create() is disabled; use rooms.booking_operations.create().")

    def bulk_update(self, objs, fields, **kwargs):
        raise TypeError("Booking.bulk_update() is disabled; use rooms.booking_operations.edit().")

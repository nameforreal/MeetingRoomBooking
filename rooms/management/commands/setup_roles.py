from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from rooms.models import Role

BOOKINGS = {"rooms.view_booking", "rooms.change_booking", "rooms.view_room"}
ROOMS = {"rooms.view_room", "rooms.add_room", "rooms.change_room"}
USERS = {"auth.view_user", "auth.change_user"}
ROLE_PERMISSIONS = {
    "Booking Viewers": {"rooms.view_booking", "rooms.view_room"},
    "Booking Managers": BOOKINGS,
    "Room Managers": ROOMS,
    "User Managers": USERS,
    "Operations Administrators": BOOKINGS | ROOMS | USERS,
}
ROLE_METADATA = {
    "Booking Viewers": ("booking-viewers", "Просмотр бронирований и каталога переговорных."),
    "Booking Managers": ("booking-managers", "Просмотр бронирований и согласование VIP-заявок."),
    "Room Managers": ("room-managers", "Создание, изменение, архивирование и восстановление переговорных."),
    "User Managers": ("user-managers", "Управление обычными учётными записями в пределах назначенных разрешений."),
    "Operations Administrators": ("operations-administrators", "Объединённые разрешения управления бронированиями, комнатами и пользователями."),
}


class Command(BaseCommand):
    help = "Create administrative roles, preserving existing permissions and memberships."

    @transaction.atomic
    def handle(self, *args, **options):
        for name, labels in ROLE_PERMISSIONS.items():
            permissions = []
            for label in sorted(labels):
                app, codename = label.split(".")
                try:
                    permissions.append(Permission.objects.get(content_type__app_label=app, codename=codename))
                except Permission.DoesNotExist as exc:
                    raise CommandError(f"Missing permission {label}; apply migrations first.") from exc
            group, created = Group.objects.get_or_create(name=name)
            group.permissions.add(*permissions)
            code, description = ROLE_METADATA[name]
            role, role_created = Role.objects.update_or_create(
                group=group,
                defaults={"code": code, "description": description},
            )
            self.stdout.write(
                f"{name}: {'created' if created else 'updated'} group, "
                f"{'created' if role_created else 'updated'} Role; {', '.join(sorted(labels))}"
            )

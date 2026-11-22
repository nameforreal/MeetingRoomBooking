from django.contrib.auth.models import Permission
from rooms.models import Role
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

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
            role, created = Role.objects.get_or_create(name=name)
            role.permissions.add(*permissions)
            self.stdout.write(f"{name}: {'created' if created else 'updated'}; {', '.join(sorted(labels))}")

from django.apps import AppConfig


class RoomsConfig(AppConfig):
    name = 'rooms'

    def ready(self):
        from django.contrib.auth import get_user_model
        from django.db.models.signals import pre_delete
        from .retention import protect_user_delete
        pre_delete.connect(protect_user_delete, sender=get_user_model(),
                           dispatch_uid="rooms.protect_user_history")

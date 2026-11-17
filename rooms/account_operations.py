"""User activity changes; a transaction-local, row/value-bound PostgreSQL permit."""
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connections, models, transaction

from . import audit
from .permissions import can_edit_account


@audit.operation_context
def save_account(user, *, actor=None, fields=None):
    alias = user._state.db or "default"
    source, actor, _ = audit.identity(actor)
    with transaction.atomic(using=alias):
        if actor is not None:
            actor = get_user_model().objects.using(alias).get(pk=actor.pk)
        if user._state.adding:
            if actor is not None and not (actor.is_active and actor.is_superuser):
                raise PermissionDenied
            models.Model.save(user, using=alias)
            return user
        queryset = get_user_model().objects.using(alias)
        lock_options = {"no_key": True} if connections[alias].vendor == "postgresql" else {}
        current = queryset.select_for_update(**lock_options).get(pk=user.pk)
        if actor is not None and not can_edit_account(actor, current):
            raise PermissionDenied
        selected = set(fields) if fields is not None else {f.name for f in user._meta.concrete_fields if not f.primary_key}
        if actor is not None and not actor.is_superuser and selected.intersection({"is_staff", "is_superuser"}):
            raise PermissionDenied
        # Never persist a stale flag from a form which did not change it.
        desired_active = user.is_active if "is_active" in selected else current.is_active
        if actor is not None and actor.pk == user.pk and not desired_active:
            raise ValidationError("Нельзя отключить собственный административный доступ.")
        if current.is_superuser and "is_staff" in selected and not user.is_staff:
            raise ValidationError("Нельзя снять права сотрудника с суперпользователя.")
        before = {"is_active": current.is_active}
        for name in selected:
            if name not in {f.name for f in user._meta.concrete_fields if not f.primary_key}:
                raise ValidationError("Недопустимое поле пользователя.")
            setattr(current, name, getattr(user, name))
        if selected:
            # Rollback of this savepoint also restores the permit on SQL failure.
            with transaction.atomic(using=alias):
                if connections[alias].vendor == "postgresql":
                    with connections[alias].cursor() as cursor:
                        cursor.execute("SELECT current_setting('meetingroom.user_activity', true), txid_current()")
                        previous, txid = cursor.fetchone()
                        permit = f"{txid}:{current.pk}:{str(current.is_active).lower()}"
                        cursor.execute("SELECT set_config('meetingroom.user_activity', %s, true)", [permit])
                models.Model.save(current, using=alias, update_fields=selected)
                if connections[alias].vendor == "postgresql":
                    with connections[alias].cursor() as cursor:
                        cursor.execute("SELECT set_config('meetingroom.user_activity', %s, true)", [previous or ""])
        if before["is_active"] != current.is_active:
            audit.record(action="user.activated" if current.is_active else "user.deactivated", obj=current,
                         before=before, after={"is_active": current.is_active}, actor=actor)
        user.__dict__.update(current.__dict__)
        return user


def set_active(user, active, *, actor=None):
    if type(active) is not bool:
        raise ValidationError("Передайте целевое состояние пользователя.")
    user.is_active = active
    return save_account(user, actor=actor, fields=["is_active"])

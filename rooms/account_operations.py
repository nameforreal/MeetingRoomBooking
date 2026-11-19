"""User activity changes; a transaction-local, row/value-bound PostgreSQL permit."""
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connections, models, transaction

from . import audit
from .permissions import can_edit_account


class ProfileUnavailable(PermissionDenied):
    """The authenticated account disappeared or was deactivated before saving."""


def validate_account_policy(current, *, actor, values):
    """Shared predictable form checks; repeated against the locked row on save."""
    if actor is not None and actor.pk == current.pk:
        if not values.get("is_active", current.is_active) or not values.get("is_staff", current.is_staff):
            raise ValidationError("Нельзя отключить собственный административный доступ.")
    if current.is_superuser and not values.get("is_staff", current.is_staff):
        raise ValidationError("Нельзя снять права сотрудника с суперпользователя.")


@audit.operation_context
def save_profile(*, actor, first_name, last_name, email):
    """Save only self-service fields, using the same row lock as deactivation."""
    if not actor.is_authenticated or actor.pk is None:
        raise ProfileUnavailable
    alias = actor._state.db or "default"
    with transaction.atomic(using=alias):
        lock_options = {"no_key": True} if connections[alias].vendor == "postgresql" else {}
        try:
            current = get_user_model().objects.using(alias).select_for_update(**lock_options).get(pk=actor.pk)
        except get_user_model().DoesNotExist as error:
            raise ProfileUnavailable from error
        if not current.is_active:
            raise ProfileUnavailable
        current.first_name = first_name
        current.last_name = last_name
        current.email = email
        # No activity permit is needed: never write security fields from actor.
        models.Model.save(current, using=alias, update_fields=["first_name", "last_name", "email"])
        return current


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
        if actor is not None and not actor.is_superuser and selected.intersection({"is_staff", "is_superuser", "password"}):
            raise PermissionDenied
        # Never persist a stale flag from a form which did not change it.
        validate_account_policy(current, actor=actor, values={
            name: getattr(user, name) for name in selected.intersection({"is_active", "is_staff"})
        })
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

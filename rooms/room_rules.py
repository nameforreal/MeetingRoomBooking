"""Room input rules shared without changing the database schema."""

from django.core.exceptions import ValidationError


def validate_capacity(value):
    if type(value) is not int or not 1 <= value <= 200:
        raise ValidationError("Вместимость должна быть целым числом от 1 до 200 человек.")

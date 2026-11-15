from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import User
from django import forms
from django.utils import timezone

from .models import Booking


class BookingForm(forms.ModelForm):
    start_time = forms.SplitDateTimeField(
        label="Начало бронирования",
        widget=forms.SplitDateTimeWidget(
            date_attrs={"type": "date"},
            time_attrs={"type": "time"},
        ),
    )

    end_time = forms.SplitDateTimeField(
        label="Конец бронирования",
        widget=forms.SplitDateTimeWidget(
            date_attrs={"type": "date"},
            time_attrs={"type": "time"},
        ),
    )

    class Meta:
        model = Booking
        fields = ["start_time", "end_time"]

    def clean(self):
        cleaned_data = super().clean()

        start = cleaned_data.get("start_time")
        end = cleaned_data.get("end_time")

        if start and end:
            if start >= end:
                raise forms.ValidationError(
                    "Время окончания должно быть позже начала."
                )

            if start < timezone.now():
                raise forms.ValidationError(
                    "Нельзя бронировать прошедшее время."
                )

        return cleaned_data

class RegisterForm(UserCreationForm):
    email = forms.EmailField(
        required=True,
        label="Электронная почта"
    )

    class Meta:
        model = User
        fields = [
            "username",
            "email",
            "password1",
            "password2",
        ]

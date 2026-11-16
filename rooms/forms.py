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
    username = forms.CharField(label='Имя пользователя', max_length=150, help_text='До 150 символов. Буквы, цифры и символы @/./+/-/_.')
    password1 = forms.CharField(label='Пароль', widget=forms.PasswordInput, help_text='Не менее 8 символов. Не используйте распространённые, слишком простые или полностью цифровые пароли.')
    password2 = forms.CharField(label='Подтверждение пароля', widget=forms.PasswordInput, help_text='Повторите пароль для проверки.')
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


class ProfileForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ["first_name", "last_name", "email"]
        labels = {
            "first_name": "Имя",
            "last_name": "Фамилия",
            "email": "Электронная почта",
        }
        widgets = {
            "first_name": forms.TextInput(attrs={"autocomplete": "given-name"}),
            "last_name": forms.TextInput(attrs={"autocomplete": "family-name"}),
            "email": forms.EmailInput(attrs={"autocomplete": "email"}),
        }

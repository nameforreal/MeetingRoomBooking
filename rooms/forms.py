from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import User
from django import forms
from django.utils import timezone

from .models import Booking, Room
from .booking_operations import parse_revision


class RevisionField(forms.Field):
    def to_python(self, value):
        if value in self.empty_values and not self.required:
            return None
        try:
            return parse_revision(value)
        except forms.ValidationError as error:
            raise forms.ValidationError(error.messages) from error


class SiteSplitDateTimeField(forms.SplitDateTimeField):
    def compress(self, data_list):
        with timezone.override(timezone.get_default_timezone()):
            try:
                return super().compress(data_list)
            except (ValueError, OverflowError) as exc:
                raise forms.ValidationError("Укажите допустимые дату и время.") from exc


class BookingForm(forms.ModelForm):
    start_time = SiteSplitDateTimeField(
        label="Начало бронирования",
        widget=forms.SplitDateTimeWidget(
            date_format="%Y-%m-%d", time_format="%H:%M:%S",
            date_attrs={"type": "date"},
            time_attrs={"type": "time", "step": "any"},
        ),
    )

    end_time = SiteSplitDateTimeField(
        label="Конец бронирования",
        widget=forms.SplitDateTimeWidget(
            date_format="%Y-%m-%d", time_format="%H:%M:%S",
            date_attrs={"type": "date"},
            time_attrs={"type": "time", "step": "any"},
        ),
    )

    class Meta:
        model = Booking
        fields = ["start_time", "end_time"]

class EditBookingForm(BookingForm):
    revision = RevisionField(widget=forms.HiddenInput)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.initial["revision"] = self.instance.revision

    room = forms.ModelChoiceField(
        queryset=Room.objects.filter(is_active=True),
        label="Переговорная комната",
        required=False,
    )

    class Meta(BookingForm.Meta):
        fields = ["room", "start_time", "end_time"]

    def clean_room(self):
        # Keep existing time-only POST clients working.
        room = self.cleaned_data.get("room") or self.instance.room
        if not room.is_active:
            raise forms.ValidationError("Комната недоступна для бронирования.")
        return room


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

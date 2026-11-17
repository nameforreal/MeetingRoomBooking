from django import forms
from django.contrib.auth import get_user_model
from rooms.models import Room

class RoomForm(forms.ModelForm):
    class Meta:
        model = Room
        fields = ["name", "description", "capacity", "is_vip", "is_active", "photo"]
        widgets = {"description": forms.Textarea(attrs={"rows": 4})}

    def clean_capacity(self):
        value = self.cleaned_data["capacity"]
        if not 1 <= value <= 200:
            raise forms.ValidationError("Вместимость должна быть от 1 до 200 человек.")
        return value

    def clean_photo(self):
        photo = self.cleaned_data.get("photo")
        if photo and hasattr(photo, "size") and photo.size > 5 * 1024 * 1024:
            raise forms.ValidationError("Размер фотографии не должен превышать 5 МБ.")
        return photo

class UserEditForm(forms.ModelForm):
    class Meta:
        model = get_user_model()
        fields = ["first_name", "last_name", "email", "is_active", "is_staff"]

    def __init__(self, *args, allow_roles=False, **kwargs):
        super().__init__(*args, **kwargs)
        if not allow_roles:
            self.fields.pop("is_staff")

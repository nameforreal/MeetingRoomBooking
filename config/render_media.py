"""Fail closed until a persistent cloud media backend is explicitly integrated."""

from django.core.exceptions import ValidationError
from django.core.files.storage import Storage
from django.http import HttpResponseBadRequest, JsonResponse

MESSAGE = "Загрузка файлов пока недоступна: постоянное облачное хранилище не подключено."


class RejectUploadsMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.FILES:
            if request.path.startswith("/api/"):
                return JsonResponse({"photo": [MESSAGE]}, status=400)
            return HttpResponseBadRequest(MESSAGE, content_type="text/plain; charset=utf-8")
        return self.get_response(request)


class UnavailableMediaStorage(Storage):
    def save(self, name, content, max_length=None):
        raise ValidationError(MESSAGE)

    def delete(self, name):
        raise ValidationError(MESSAGE)

    def exists(self, name):
        return False

    def _open(self, name, mode="rb"):
        raise FileNotFoundError(MESSAGE)

    def url(self, name):
        # Existing/imported media names must never become local disk URLs.
        from django.templatetags.static import static

        return static("rooms/images/space1.jpg")

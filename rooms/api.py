import re

from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils import timezone
from rest_framework import serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.response import Response

from .models import Room, Booking
from . import booking_operations as operations
from .permissions import RoomPermissions, staff_has_perm
from . import booking_rules as rules
from .booking_idempotency import IdempotencyError, validate_key


def query_date(params, key, *, booking=False):
    try:
        if key not in params and not booking:
            return None
        parser = rules.booking_date if booking else rules.parse_calendar_date
        return parser(params.get(key))
    except DjangoValidationError as exc:
        raise serializers.ValidationError({key: exc.messages}) from exc


class SiteDateTimeField(serializers.DateTimeField):
    def default_timezone(self):
        return timezone.get_default_timezone()

    def to_internal_value(self, value):
        if isinstance(value, str) and ("T" in value or " " in value):
            offset = re.search(r"[+-](\d{2})(?::?(\d{2}))?(?::?(\d{2}))?(?:[.,]\d+)?$", value)
            if offset and (int(offset[1]) >= 24 or any(int(part or 0) >= 60 for part in offset.groups()[1:])):
                self.fail("invalid", format="ISO 8601")
        try:
            return super().to_internal_value(value)
        except (OverflowError, ValueError, TypeError):
            self.fail("invalid", format="ISO 8601")


class RoomSerializer(serializers.ModelSerializer):
    def update(self, instance, validated_data):
        try:
            return super().update(instance, validated_data)
        except DjangoValidationError as exc:
            raise serializers.ValidationError({"room": exc.messages}) from exc

    class Meta:
        model = Room
        fields = ["id", "name", "description", "capacity", "is_vip", "is_active", "created_at"]
        read_only_fields = ["id", "created_at"]


class RevisionSerializerField(serializers.IntegerField):
    def to_internal_value(self, value):
        try:
            return operations.parse_revision(value)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.messages) from exc


class BookingRevisionSerializer(serializers.Serializer):
    revision = RevisionSerializerField()


class BookingCreateSerializer(serializers.Serializer):
    # Syntax only: dynamic business checks must follow receipt lookup.
    room = serializers.IntegerField(min_value=1, max_value=9223372036854775807)
    start_time = SiteDateTimeField()
    end_time = SiteDateTimeField()


class BookingSerializer(serializers.ModelSerializer):
    start_time = SiteDateTimeField()
    end_time = SiteDateTimeField()
    revision = RevisionSerializerField(required=False)
    room_name = serializers.CharField(source="room.name", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = Booking
        fields = [
            "id", "room", "room_name", "start_time", "end_time",
            "status", "status_display", "created_at", "revision",
        ]
        read_only_fields = ["id", "status", "status_display", "created_at"]

    def validate_room(self, room):
        if not room.is_active:
            raise serializers.ValidationError("Комната недоступна для бронирования.")
        return room

    def validate(self, attrs):
        instance = self.instance
        if instance is not None:
            try:
                attrs["revision"] = operations.parse_revision(attrs.get("revision"))
            except DjangoValidationError as exc:
                raise serializers.ValidationError(exc.message_dict) from exc
        room = attrs.get("room", getattr(instance, "room", None))
        start = attrs.get("start_time", getattr(instance, "start_time", None))
        end = attrs.get("end_time", getattr(instance, "end_time", None))
        if not (room and start and end):
            return attrs

        candidate = Booking(
            pk=instance.pk if instance else None,
            room=room,
            user=self.context["request"].user,
            start_time=start,
            end_time=end,
            status=instance.status if instance else ("pending" if room.is_vip else "confirmed"),
        )
        try:
            candidate.clean()
        except DjangoValidationError as exc:
            raise serializers.ValidationError({"booking": exc.messages}) from exc
        return attrs

    def create(self, validated_data):
        raise TypeError("Use BookingViewSet.create() and operations.create_once().")

    def update(self, instance, validated_data):
        try:
            return operations.edit(instance, actor=self.context["request"].user, **validated_data)
        except DjangoValidationError as exc:
            raise serializers.ValidationError({"booking": exc.messages}) from exc


class RoomViewSet(viewsets.ModelViewSet):
    serializer_class = RoomSerializer
    http_method_names = ["get", "post", "put", "patch", "delete", "head", "options"]

    permission_classes = [RoomPermissions]

    def get_queryset(self):
        queryset = Room.objects.all().order_by("name")
        if self.action in {"update", "partial_update", "destroy"}:
            return queryset
        if (self.action in {"list", "retrieve"}
                and self.request.query_params.get("include_archived") == "true"):
            from rest_framework.exceptions import PermissionDenied
            if not staff_has_perm(self.request.user, "rooms.view_room"):
                raise PermissionDenied()
            return queryset
        return queryset.filter(is_active=True)

    def destroy(self, request, *args, **kwargs):
        # Preserve historical bookings: remove a room from the public catalog instead of deleting it.
        room = self.get_object()
        room.is_active = False
        try:
            room.save(update_fields=["is_active"])
        except DjangoValidationError as exc:
            raise serializers.ValidationError({"room": exc.messages}) from exc
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=["get"], permission_classes=[AllowAny])
    def availability(self, request, pk=None):
        room = self.get_object()
        selected = query_date(request.query_params, "date", booking=True)
        start, end = rules.day_bounds(selected)
        bookings = Booking.objects.filter(
            room=room,
            status__in=["pending", "confirmed"],
            start_time__lt=end,
            end_time__gt=start,
        ).order_by("start_time")
        return Response({
            "room": room.pk,
            "room_name": room.name,
            "date": selected.isoformat(),
            "busy_intervals": [
                {"start_time": item.start_time, "end_time": item.end_time}
                for item in bookings
            ],
        })


class BookingViewSet(viewsets.ModelViewSet):
    serializer_class = BookingSerializer
    permission_classes = [IsAuthenticated]
    http_method_names = ["get", "post", "put", "patch", "head", "options"]

    def create(self, request, *args, **kwargs):
        try:
            key = validate_key(request.headers.get("Idempotency-Key"))
            serializer = BookingCreateSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            data = serializer.validated_data
            result = operations.create_once(actor=request.user, key=key, room_id=data["room"],
                                            start_time=data["start_time"], end_time=data["end_time"])
        except IdempotencyError as exc:
            headers = {"Retry-After": "1"} if exc.status == 503 else {}
            return Response({"code": exc.code, "detail": exc.messages[0]}, status=exc.status, headers=headers)
        except DjangoValidationError as exc:
            return Response({"booking": exc.messages}, status=400)
        return Response(result.payload, status=result.status,
                        headers={"Idempotency-Replayed": "true" if result.replayed else "false"})

    def get_queryset(self):
        queryset = Booking.objects.filter(user=self.request.user).select_related("room")
        params = self.request.query_params
        status_filter = params.get("status")
        if status_filter in {choice[0] for choice in Booking.STATUS_CHOICES}:
            queryset = queryset.filter(status=status_filter)
        room_id = params.get("room")
        if room_id and room_id.isdigit():
            queryset = queryset.filter(room_id=int(room_id))
        date_from = query_date(params, "date_from")
        date_to = query_date(params, "date_to")
        if date_from and date_to and date_to < date_from:
            raise serializers.ValidationError({"date_to": ["Конец периода должен быть не раньше начала."]})
        if date_from:
            queryset = queryset.filter(start_time__date__gte=date_from)
        if date_to:
            queryset = queryset.filter(start_time__date__lte=date_to)
        period = params.get("period")
        now = timezone.now()
        if period == "upcoming":
            queryset = queryset.filter(end_time__gte=now).exclude(status="cancelled")
        elif period == "past":
            queryset = queryset.filter(end_time__lt=now).exclude(status="cancelled")
        return queryset.order_by(params.get("ordering", "-start_time") if params.get("ordering") in {"start_time", "-start_time", "created_at", "-created_at"} else "-start_time")

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        booking = self.get_object()
        payload = BookingRevisionSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        try:
            booking = operations.cancel(booking, actor=request.user, revision=payload.validated_data["revision"])
        except DjangoValidationError as exc:
            return Response({"booking": exc.messages}, status=400)
        return Response(self.get_serializer(booking).data, status=status.HTTP_200_OK)

import csv
from datetime import datetime, time, timedelta

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework import serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated, AllowAny, IsAdminUser
from rest_framework.response import Response

from .models import Room, Booking


class RoomSerializer(serializers.ModelSerializer):
    class Meta:
        model = Room
        fields = ["id", "name", "description", "capacity", "is_vip", "is_active", "created_at"]
        read_only_fields = ["id", "created_at"]


class BookingSerializer(serializers.ModelSerializer):
    room_name = serializers.CharField(source="room.name", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = Booking
        fields = [
            "id", "room", "room_name", "start_time", "end_time",
            "status", "status_display", "created_at",
        ]
        read_only_fields = ["id", "status", "status_display", "created_at"]

    def validate_room(self, room):
        if not room.is_active:
            raise serializers.ValidationError("Комната недоступна для бронирования.")
        return room

    def validate(self, attrs):
        instance = self.instance
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
        room = validated_data["room"]
        try:
            with transaction.atomic():
                locked_room = Room.objects.select_for_update().get(pk=room.pk)
                if not locked_room.is_active:
                    raise serializers.ValidationError({"room": "Комната недоступна для бронирования."})
                booking = Booking(user=self.context["request"].user, **validated_data)
                booking.save()
                return booking
        except DjangoValidationError as exc:
            raise serializers.ValidationError({"booking": exc.messages}) from exc

    def update(self, instance, validated_data):
        try:
            with transaction.atomic():
                locked = Booking.objects.select_for_update().get(pk=instance.pk)
                room = validated_data.get("room", locked.room)
                Room.objects.select_for_update().get(pk=room.pk)
                if not room.is_active:
                    raise serializers.ValidationError({"room": "Комната недоступна для бронирования."})
                for field, value in validated_data.items():
                    setattr(locked, field, value)
                locked.save()
                return locked
        except DjangoValidationError as exc:
            raise serializers.ValidationError({"booking": exc.messages}) from exc


class RoomViewSet(viewsets.ModelViewSet):
    serializer_class = RoomSerializer
    http_method_names = ["get", "post", "put", "patch", "delete", "head", "options"]

    def get_permissions(self):
        if self.request.method in ("GET", "HEAD", "OPTIONS"):
            return [AllowAny()]
        return [IsAdminUser()]

    def get_queryset(self):
        return Room.objects.filter(is_active=True).order_by("name")

    def destroy(self, request, *args, **kwargs):
        # Preserve historical bookings: remove a room from the public catalog instead of deleting it.
        room = self.get_object()
        room.is_active = False
        room.save(update_fields=["is_active"])
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=["get"], permission_classes=[AllowAny])
    def availability(self, request, pk=None):
        room = self.get_object()
        try:
            selected = parse_date(request.query_params.get("date", ""))
        except ValueError:
            selected = None
        if not selected:
            return Response({"date": "Укажите дату в формате YYYY-MM-DD."}, status=400)
        start = timezone.make_aware(datetime.combine(selected, time.min))
        end = start + timedelta(days=1)
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

    def get_queryset(self):
        queryset = Booking.objects.filter(user=self.request.user).select_related("room")
        params = self.request.query_params
        status_filter = params.get("status")
        if status_filter in {choice[0] for choice in Booking.STATUS_CHOICES}:
            queryset = queryset.filter(status=status_filter)
        room_id = params.get("room")
        if room_id and room_id.isdigit():
            queryset = queryset.filter(room_id=int(room_id))
        date_from = parse_date(params.get("date_from", ""))
        date_to = parse_date(params.get("date_to", ""))
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
        with transaction.atomic():
            booking = get_object_or_404(self.get_queryset().select_for_update(), pk=pk)
            if booking.status == "cancelled":
                return Response(self.get_serializer(booking).data)
            if booking.end_time <= timezone.now():
                return Response({"detail": "Нельзя отменить уже завершившуюся встречу."}, status=400)
            booking.status = "cancelled"
            booking.save(update_fields=["status"])
        return Response(self.get_serializer(booking).data, status=status.HTTP_200_OK)

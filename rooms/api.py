from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework import serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.response import Response
from .models import Room, Booking


class RoomSerializer(serializers.ModelSerializer):
    class Meta:
        model = Room
        fields = ["id", "name", "description", "capacity", "is_vip"]


class BookingSerializer(serializers.ModelSerializer):
    room_name = serializers.CharField(source="room.name", read_only=True)

    class Meta:
        model = Booking
        fields = ["id", "room", "room_name", "start_time", "end_time", "status", "created_at"]
        read_only_fields = ["id", "room_name", "status", "created_at"]

    def validate_room(self, room):
        if not room.is_active:
            raise serializers.ValidationError("Комната недоступна для бронирования.")
        return room

    def create(self, validated_data):
        room = validated_data["room"]
        try:
            with transaction.atomic():
                Room.objects.select_for_update().get(pk=room.pk)
                booking = Booking(user=self.context["request"].user, **validated_data)
                booking.save()
                return booking
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.messages) from exc


class RoomViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Room.objects.filter(is_active=True)
    serializer_class = RoomSerializer
    permission_classes = [AllowAny]


class BookingViewSet(viewsets.ModelViewSet):
    serializer_class = BookingSerializer
    permission_classes = [IsAuthenticated]
    http_method_names = ["get", "post", "head", "options"]

    def get_queryset(self):
        return Booking.objects.filter(user=self.request.user).select_related("room").order_by("-start_time")

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        with transaction.atomic():
            booking = get_object_or_404(self.get_queryset().select_for_update(), pk=pk)
            if booking.status != "cancelled":
                booking.status = "cancelled"
                booking.save(update_fields=["status"])
        return Response(self.get_serializer(booking).data, status=status.HTTP_200_OK)

"""The administrative list and its CSV export must select the same records."""
from django.db.models import Q

from rooms.models import Booking


def filtered_bookings(params):
    status = params.get("status", "")
    status = status if status in {choice[0] for choice in Booking.STATUS_CHOICES} else ""
    term = params.get("q", "").strip()[:100]
    queryset = Booking.objects.select_related("room", "user")
    if status:
        queryset = queryset.filter(status=status)
    if term:
        queryset = queryset.filter(Q(room__name__icontains=term) | Q(user__username__icontains=term)
                                   | Q(user__email__icontains=term))
    return queryset, status, term

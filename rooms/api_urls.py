from rest_framework.routers import DefaultRouter
from .api import RoomViewSet, BookingViewSet

router = DefaultRouter()
router.register("rooms", RoomViewSet, basename="api-room")
router.register("bookings", BookingViewSet, basename="api-booking")
urlpatterns = router.urls

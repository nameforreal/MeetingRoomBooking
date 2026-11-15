from django.contrib.auth import views as auth_views
from django.urls import path
from . import views

urlpatterns = [
    path("", views.home, name="home"),

    path(
        "book/<int:room_id>/",
        views.book_room,
        name="book_room"
    ),

    path(
        "my-bookings/",
        views.my_bookings,
        name="my_bookings"
    ),

    path(
        "cancel/<int:booking_id>/",
        views.cancel_booking,
        name="cancel_booking"
    ),

    path(
        "register/",
        views.register,
        name="register"
    ),

    path(
        "login/",
        auth_views.LoginView.as_view(
            template_name="rooms/login.html"
        ),
        name="login"
    ),

    path(
        "logout/",
        auth_views.LogoutView.as_view(),
        name="logout"
    ),

]

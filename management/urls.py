from django.urls import path
from . import views
app_name='management'
urlpatterns=[path('',views.overview,name='overview'),path('bookings/',views.bookings,name='bookings'),path('bookings/<int:pk>/status/',views.status,name='status'),path('rooms/',views.rooms,name='rooms'),path('users/',views.users,name='users'),path('users/<int:pk>/edit/',views.user_edit,name='user_edit'),path('rooms/add/',views.room_edit,name='room_add'),path('rooms/<int:pk>/edit/',views.room_edit,name='room_edit'),path('rooms/<int:pk>/toggle/',views.room_toggle,name='room_toggle'),path('analytics/',views.analytics,name='analytics'),path('export.csv',views.export_csv,name='export_csv')]

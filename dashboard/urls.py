from django.urls import path
from . import views
urlpatterns = [path('dashboard/', views.overview, name='dashboard'), path('schedule/', views.schedule, name='schedule')]

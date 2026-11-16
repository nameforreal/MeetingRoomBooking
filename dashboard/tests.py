from datetime import timedelta
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rooms.models import Room, Booking

class DashboardTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username='dashboard_tester', password='StrongPass123!')
        self.room = Room.objects.create(name='Test schedule', capacity=6)

    def test_requires_login(self):
        self.assertEqual(self.client.get(reverse('dashboard')).status_code, 302)
        self.assertEqual(self.client.get(reverse('schedule')).status_code, 302)

    def test_dashboard_and_schedule(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse('dashboard')).status_code, 200)
        self.assertEqual(self.client.get(reverse('schedule')).status_code, 200)

    def test_schedule_shows_occupied_slot(self):
        self.client.force_login(self.user)
        start = timezone.localtime(timezone.now() + timedelta(days=2)).replace(hour=10,minute=0,second=0,microsecond=0)
        Booking.objects.create(room=self.room,user=self.user,start_time=start,end_time=start+timedelta(hours=1))
        response = self.client.get(reverse('schedule'), {'date': start.date().isoformat()})
        self.assertContains(response, 'ent-busy')

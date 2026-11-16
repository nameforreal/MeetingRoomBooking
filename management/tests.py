from datetime import timedelta
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rooms.models import Booking,Room
class ManagementTests(TestCase):
 def setUp(self):
  U=get_user_model(); self.staff=U.objects.create_user(username='manager',password='StrongPass123!',is_staff=True); self.user=U.objects.create_user(username='member',password='StrongPass123!')
  self.room=Room.objects.create(name='Manager room',capacity=6); start=timezone.now()+timedelta(days=3); self.booking=Booking.objects.create(room=self.room,user=self.user,start_time=start,end_time=start+timedelta(hours=1))
 def test_staff_only(self):
  self.assertEqual(self.client.get(reverse('management:overview')).status_code,302); self.client.force_login(self.user); self.assertEqual(self.client.get(reverse('management:overview')).status_code,302)
 def test_staff_pages(self):
  self.client.force_login(self.staff)
  for name in ('management:overview','management:bookings','management:rooms','management:users'): self.assertEqual(self.client.get(reverse(name)).status_code,200)
 def test_regular_user_cannot_change_status(self):
  self.client.force_login(self.user); self.client.post(reverse('management:status',args=[self.booking.pk]),{'status':'cancelled'}); self.booking.refresh_from_db(); self.assertNotEqual(self.booking.status,'cancelled')
 def test_csv_export(self):
  self.client.force_login(self.staff); r=self.client.get(reverse('management:export_csv')); self.assertEqual(r.status_code,200); self.assertIn('Manager room',r.content.decode('utf-8-sig'))


class ExtendedManagementTests(TestCase):
 def setUp(self):
  U=get_user_model()
  self.staff=U.objects.create_user(username="staff2", password="TestPass123!", is_staff=True)
  self.admin=U.objects.create_superuser(username="admin2", password="TestPass123!", email="admin@example.com")
  self.member=U.objects.create_user(username="member2", password="TestPass123!")
  self.room=Room.objects.create(name="Test room", capacity=4)

 def test_staff_creates_and_edits_room(self):
  self.client.force_login(self.staff)
  response=self.client.post(reverse("management:room_add"), {"name":"Created room", "capacity":8,"description":"New","is_active":"on"})
  self.assertEqual(response.status_code,302)
  room=Room.objects.get(name="Created room")
  self.assertEqual(room.capacity,8)
  self.client.post(reverse("management:room_edit",args=[room.pk]),{"name":"Updated room","capacity":12,"description":"Updated","is_active":"on"})
  room.refresh_from_db()
  self.assertEqual(room.capacity,12)

 def test_staff_can_toggle_room_and_regular_cannot(self):
  url=reverse("management:room_toggle",args=[self.room.pk])
  self.client.force_login(self.member)
  self.client.post(url)
  self.room.refresh_from_db()
  self.assertTrue(self.room.is_active)
  self.client.force_login(self.staff)
  self.assertEqual(self.client.get(url).status_code,405)
  self.client.post(url)
  self.room.refresh_from_db()
  self.assertFalse(self.room.is_active)

 def test_only_superuser_can_edit_accounts(self):
  url=reverse("management:user_edit",args=[self.member.pk])
  self.client.force_login(self.staff)
  self.assertEqual(self.client.get(url).status_code,403)
  self.client.force_login(self.admin)
  self.assertEqual(self.client.get(url).status_code,200)
  self.client.post(url,{"first_name":"Edited","last_name":"User","email":"","is_active":"on","is_staff":"on"})
  self.member.refresh_from_db()
  self.assertTrue(self.member.is_staff)

 def test_superuser_cannot_disable_self(self):
  self.client.force_login(self.admin)
  url=reverse("management:user_edit",args=[self.admin.pk])
  self.client.post(url,{"first_name":"","last_name":"","email":"admin@example.com"})
  self.admin.refresh_from_db()
  self.assertTrue(self.admin.is_active)
  self.assertTrue(self.admin.is_staff)

 def test_analytics_and_date_validation(self):
  self.client.force_login(self.staff)
  url=reverse("management:analytics")
  self.assertEqual(self.client.get(url).status_code,200)
  self.assertEqual(self.client.get(url,{"from":"bad","to":"also-bad"}).status_code,200)

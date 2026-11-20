"""Catalog-only numeric ordering and filter regressions."""

from django.test import TestCase
from django.urls import reverse

from rooms.models import Room


class CatalogOrderingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.rooms = {}
        # Deliberately create in an order different from the room numbers.
        for number, capacity, vip in (
            (5, 10, False),
            (2, 8, True),
            (4, 4, False),
            (1, 2, False),
            (3, 12, True),
        ):
            cls.rooms[number] = Room.objects.create(
                name=f"Переговорная №{number}",
                capacity=capacity,
                is_vip=vip,
                description="Проектор" if vip else "Доска",
            )

    def catalog(self, params=None):
        response = self.client.get(reverse("catalog"), params or {})
        self.assertEqual(response.status_code, 200)
        return response

    def assert_rooms(self, response, expected):
        self.assertEqual(
            [room.pk for room in response.context["rooms"]], [room.pk for room in expected]
        )
        html = response.content.decode()
        positions = [html.index(f"<h3>{room.name}</h3>") for room in expected]
        self.assertEqual(positions, sorted(positions))

    def test_five_rooms_are_rendered_in_numeric_order_not_creation_order(self):
        original = dict(Room.objects.values_list("pk", "name"))
        self.assert_rooms(self.catalog(), [self.rooms[n] for n in range(1, 6)])
        self.assertEqual(dict(Room.objects.values_list("pk", "name")), original)

    def test_double_digit_numbers_follow_single_digit_numbers(self):
        tenth = Room.objects.create(name="Переговорная №10")
        twelfth = Room.objects.create(name="Переговорная №12")
        self.assert_rooms(self.catalog(), [self.rooms[n] for n in range(1, 6)] + [tenth, twelfth])

    def test_numeric_value_overrides_name_prefix_spaces_and_leading_zeroes(self):
        tenth = Room.objects.create(name="Альфа №010")
        sixth = Room.objects.create(name="Янтарь № 006")
        self.assert_rooms(self.catalog(), [self.rooms[n] for n in range(1, 6)] + [sixth, tenth])

    def test_unnumbered_rooms_follow_numbered_rooms(self):
        zenith = Room.objects.create(name="Zenith")
        atlas = Room.objects.create(name="Atlas")
        self.assert_rooms(self.catalog(), [self.rooms[n] for n in range(1, 6)] + [atlas, zenith])

    def test_name_and_description_search_preserve_numeric_order(self):
        tenth = Room.objects.create(name="Переговорная №10", description="Проектор")
        self.assert_rooms(self.catalog({"q": "№2"}), [self.rooms[2]])
        response = self.catalog({"q": "  Проектор  "})
        self.assert_rooms(response, [self.rooms[2], self.rooms[3], tenth])
        self.assertEqual(response.context["query"], "Проектор")

    def test_capacity_and_category_filters_keep_numeric_order(self):
        self.assert_rooms(
            self.catalog({"capacity": "8"}), [self.rooms[2], self.rooms[3], self.rooms[5]]
        )
        self.assert_rooms(self.catalog({"type": "vip"}), [self.rooms[2], self.rooms[3]])
        self.assert_rooms(
            self.catalog({"type": "standard"}), [self.rooms[1], self.rooms[4], self.rooms[5]]
        )
        self.assert_rooms(
            self.catalog({"q": "Проектор", "capacity": "10", "type": "vip"}), [self.rooms[3]]
        )

    def test_inactive_rooms_remain_hidden_and_empty_results_work(self):
        Room.objects.create(name="Переговорная №0", is_active=False)
        response = self.catalog()
        self.assert_rooms(response, [self.rooms[n] for n in range(1, 6)])
        self.assertEqual(response.context["total_rooms"], 5)
        self.assert_rooms(self.catalog({"q": "Несуществующая"}), [])

    def test_default_model_ordering_and_home_featured_rooms_are_unchanged(self):
        tenth = Room.objects.create(name="Переговорная №10")
        self.assertEqual(
            list(Room.objects.all()),
            [self.rooms[1], tenth, self.rooms[2], self.rooms[3], self.rooms[4], self.rooms[5]],
        )
        response = self.client.get(reverse("home"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.context["featured"],
            list(Room.objects.filter(is_active=True).order_by("-created_at")[:3]),
        )

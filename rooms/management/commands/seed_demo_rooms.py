from django.core.management.base import BaseCommand
from rooms.models import Room

# Demonstration data; never overwrites or deletes existing records.
ROOMS = [
    ("Atlas · Conference", "Просторный зал для презентаций, демонстраций и командных совещаний. Экран, видеосвязь, маркерная доска.", 12, False),
    ("Focus · Studio", "Камерное пространство для интервью и переговоров один на один. Тихая атмосфера и монитор.", 2, False),
    ("Horizon · Boardroom", "Представительский зал для стратегических сессий. Большой экран и система видеоконференций.", 16, True),
    ("Nova · Workshop", "Гибкая переговорная для мозговых штурмов и воркшопов. Доска и большой стол.", 8, False),
    ("Lumen · Meeting", "Светлая комната для ежедневных синхронизаций. Монитор и удобная посадка.", 6, False),
    ("Summit · Executive", "Премиальное пространство для руководящих встреч и переговоров с партнёрами.", 10, True),
    ("Orbit · Team", "Универсальная комната для проектных команд. Экран и оборудование для гибридных встреч.", 8, False),
    ("Nexus · Strategy", "Комната для стратегического планирования, презентаций и обсуждения важных решений.", 14, True),
    ("Pulse · Huddle", "Компактная переговорная для коротких встреч и созвонов. Монитор и звукоизоляция.", 4, False),
]

class Command(BaseCommand):
    help = "Add 9 example meeting rooms without modifying existing rooms or bookings."

    def handle(self, *args, **options):
        created_count = 0
        for name, description, capacity, is_vip in ROOMS:
            _, created = Room.objects.get_or_create(name=name, defaults={
                "description": description, "capacity": capacity, "is_vip": is_vip, "is_active": True,
            })
            created_count += int(created)
        self.stdout.write(self.style.SUCCESS(f"Добавлено комнат: {created_count}. Уже существующие не изменены."))

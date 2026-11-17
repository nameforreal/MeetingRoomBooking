from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("rooms", "0003_room_photo")]
    operations = [migrations.AlterModelOptions(
        name="booking",
        options={"ordering": ["-start_time"],
                 "verbose_name": '\u0411\u0440\u043e\u043d\u0438\u0440\u043e\u0432\u0430\u043d\u0438\u0435',
                 "verbose_name_plural": '\u0411\u0440\u043e\u043d\u0438\u0440\u043e\u0432\u0430\u043d\u0438\u044f',
                 "permissions": [("export_booking", "Can export bookings")]},
    )]

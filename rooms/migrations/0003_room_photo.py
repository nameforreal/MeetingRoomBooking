from django.db import migrations, models

class Migration(migrations.Migration):
    dependencies = [("rooms", "0002_booking")]
    operations = [migrations.AddField(model_name="room", name="photo", field=models.ImageField(blank=True, null=True, upload_to="room_photos/", verbose_name="Фотография"))]

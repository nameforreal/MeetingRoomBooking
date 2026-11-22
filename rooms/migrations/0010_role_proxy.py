from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("auth", "0012_alter_user_first_name_max_length"),
        ("rooms", "0009_user_activity_guard"),
    ]

    operations = [
        migrations.CreateModel(
            name="Role",
            fields=[],
            options={
                "verbose_name": "Роль",
                "verbose_name_plural": "Роли",
                "proxy": True,
                "default_permissions": (),
            },
            bases=("auth.group",),
        ),
    ]

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("treks", "0007_expand_trek_model"),
    ]

    operations = [
        migrations.AddField(
            model_name="trek",
            name="elevation_profile",
            field=models.JSONField(
                blank=True,
                null=True,
                help_text="Day-by-day altitude in metres, e.g. [2000, 2800, 3400, 3800, 3200, 2000]. "
                          "Populated by scraping or manual entry.",
            ),
        ),
    ]

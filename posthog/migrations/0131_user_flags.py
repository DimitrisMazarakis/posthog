# Generated by Django 3.0.11 on 2021-03-04 23:42

import django.contrib.postgres.fields.jsonb
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("posthog", "0130_dashboard_creation_mode"),
    ]

    operations = [
        migrations.AddField(
            model_name="user", name="flags", field=django.contrib.postgres.fields.jsonb.JSONField(default=dict),
        ),
    ]
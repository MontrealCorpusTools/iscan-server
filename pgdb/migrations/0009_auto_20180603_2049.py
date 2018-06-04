# Generated by Django 2.0 on 2018-06-03 20:49

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('pgdb', '0008_auto_20180530_0215'),
    ]

    operations = [
        migrations.AddField(
            model_name='enrichment',
            name='completed',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='enrichment',
            name='last_run',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
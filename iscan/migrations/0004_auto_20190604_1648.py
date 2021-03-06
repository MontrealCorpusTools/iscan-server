# Generated by Django 2.0 on 2019-06-04 21:48

from django.db import migrations, models

def create_default_perms(apps, schema_editor):
    Corpus = apps.get_model("iscan", "Corpus")
    CorpusPermissions = apps.get_model("iscan", "CorpusPermissions")
    User = apps.get_model('auth', 'User')
    for user in User.objects.all():
        for corpus in Corpus.objects.all():
            if 'tutorial' in corpus.name.lower() and corpus.name.split('-')[-1] == user.username and \
                    corpus.corpus_type != 'T':
                corpus.corpus_type = 'T'
                corpus.save()
            perm, created = CorpusPermissions.objects.get_or_create(user=user, corpus=corpus)
            if not created:
                perm.can_query = True
                perm.save()

class Migration(migrations.Migration):

    dependencies = [
        ('iscan', '0003_auto_20190530_0600'),
    ]

    operations = [
        migrations.AddField(
            model_name='corpuspermissions',
            name='can_query',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='corpuspermissions',
            name='is_whitelist_exempt',
            field=models.BooleanField(default=False),
        ),
        migrations.AlterField(
            model_name='corpus',
            name='corpus_type',
            field=models.CharField(choices=[('T', 'Tutorial'), ('P', 'Public'), ('N', 'Private (non-restricted)'), ('R', 'Restricted')], default='R', max_length=1),
        ),
        migrations.AlterField(
            model_name='profile',
            name='user_type',
            field=models.CharField(choices=[('G', 'Guest'), ('A', 'Annotator'), ('R', 'Researcher'), ('U', 'Unlimited researcher')], default='G', max_length=1),
        ),
        migrations.RunPython(create_default_perms),
    ]

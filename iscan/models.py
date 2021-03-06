import os
import sys
import subprocess
import traceback
import signal
import json
import time
import logging
import csv
import socket
import yaml
import shutil
import datetime

from django.db import models
from django.conf import settings
from django.contrib.auth.models import Group, User
from django.db.models.signals import post_save
from django.dispatch import receiver

from celery.result import AsyncResult

# Comment out once PolyglotDB docker compatibility is merged
sys.path.insert(0, '/site/proj/PolyglotDB')

from polyglotdb import CorpusContext
import polyglotdb.io as pgio
from polyglotdb \
    .config import CorpusConfig
from polyglotdb.utils import get_corpora_list

from .utils import download_influxdb, download_neo4j, extract_influxdb, extract_neo4j, make_influxdb_safe, get_pids, \
    get_used_ports, is_port_in_use, run_spade_script

import logging

log = logging.getLogger(__name__)


# Create your models here.

class Profile(models.Model):
    GUEST = 'G'
    ANNOTATOR = 'A'
    RESEARCHER = 'R'
    UNLIMITED = 'U'
    TYPE_CHOICES = (
        (GUEST, 'Guest'),
        (ANNOTATOR, 'Annotator'),
        (RESEARCHER, 'Researcher'),
        (UNLIMITED, 'Unlimited researcher'),
    )
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    user_type = models.CharField(max_length=1, choices=TYPE_CHOICES, default=GUEST)

    @property
    def tutorial_corpus_directory(self):
        user_tutorial_name = 'tutorial-{}'.format(self.user.username)
        return os.path.join(settings.SOURCE_DATA_DIRECTORY, user_tutorial_name)

    @property
    def has_tutorial_corpus(self):
        return os.path.exists(self.tutorial_corpus_directory)

    def create_tutorial_corpus(self):
        tutorial_name = 'tutorial'
        base_dir = os.path.join(settings.SOURCE_DATA_DIRECTORY, tutorial_name)
        user_tutorial_name = 'tutorial-{}'.format(self.user.username)
        if self.has_tutorial_corpus:
            return
        if not os.path.exists(base_dir): # Hacky tutorial corpus with access to SPADE
            git_string = 'git+ssh://{}@{}:{}'.format(settings.SPADE_CONFIG['user'],
                                                      settings.SPADE_CONFIG['host'],
                                                      os.path.join(settings.SPADE_CONFIG['base_path'], 'spade-tutorial'))
            subprocess.call(['git', 'clone', git_string], cwd=settings.SOURCE_DATA_DIRECTORY)
            os.rename(os.path.join(settings.SOURCE_DATA_DIRECTORY, 'spade-tutorial'), base_dir)
        try:
            shutil.copytree(base_dir, self.tutorial_corpus_directory, copy_function=shutil.copyfile)
        except shutil.Error:
            if not self.has_tutorial_corpus:
                raise
        d, _ = Database.objects.get_or_create(name=user_tutorial_name)
        c, _ = Corpus.objects.get_or_create(name=user_tutorial_name, database=d, corpus_type=Corpus.TUTORIAL)
        return c

    def get_tutorial_corpus(self):
        user_tutorial_name = 'tutorial-{}'.format(self.user.username)
        try:
            return Corpus.objects.get(name=user_tutorial_name)
        except Corpus.DoesNotExist:
            return None

    def update_role_permissions(self):
        for perm in self.user.corpus_permissions.all():
            perm.set_role_permissions()
            perm.save()


@receiver(post_save, sender=User)
def create_user_profile(sender, instance, created, **kwargs):
    if created:
        if instance.is_superuser:
            user_type = 'U'
        else:
            user_type = Profile.GUEST
        profile = Profile.objects.create(user=instance, user_type=user_type)
        new_permissions = []
        for c in Corpus.objects.all():
            perm = CorpusPermissions(corpus=c, user=instance)
            new_permissions.append(perm)
        if new_permissions:
            CorpusPermissions.objects.bulk_create(new_permissions)


@receiver(post_save, sender=User)
def save_user_profile(sender, instance, **kwargs):
    instance.profile.save()


class Database(models.Model):
    """
    Database objects contain meta data about the PolyglotDB databases, namely the Neo4j graph database and the InfluxDB
    time series database.

    Database objects are the way that Neo4j and InfluxDB get installed, started and stopped.
    """
    RUNNING = 'R'
    STOPPED = 'S'
    ERROR = 'E'
    STATUS_CHOICES = (
        (STOPPED, 'Stopped'),
        (RUNNING, 'Running'),
        (ERROR, 'Error'),
    )
    name = models.CharField(max_length=100, unique=True)
    neo4j_http_port = models.SmallIntegerField(blank=True)
    neo4j_https_port = models.SmallIntegerField(blank=True)
    neo4j_bolt_port = models.SmallIntegerField(blank=True)
    neo4j_admin_port = models.SmallIntegerField(blank=True)
    influxdb_http_port = models.SmallIntegerField(blank=True)
    influxdb_meta_port = models.SmallIntegerField(blank=True)
    influxdb_udp_port = models.SmallIntegerField(blank=True)
    influxdb_admin_port = models.SmallIntegerField(blank=True)
    status = models.CharField(max_length=1, choices=STATUS_CHOICES, default=STOPPED)
    neo4j_pid = models.IntegerField(null=True, blank=True)
    influxdb_pid = models.IntegerField(null=True, blank=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name

    @property
    def num_corpora(self):
        return self.corpora.count()

    @property
    def directory(self):
        """
        Data directory to store all databases and corpora data

        :return:
        """
        return os.path.join(settings.POLYGLOT_DATA_DIRECTORY, self.name)

    @property
    def neo4j_exe_path(self):
        """
        The path to the Neo4j executable that will start and stop the graph database.

        :return:
        """
        exe_name = 'neo4j'
        if sys.platform.startswith('win'):
            exe_name += '.bat'
        return os.path.abspath(os.path.join(self.directory, 'neo4j', 'bin', exe_name))

    @property
    def influxdb_exe_path(self):
        """
        The path to the InfluxDB executable (or command on Mac) that will start the time series database (stopping is
        done through sending an interrupt signal).

        :return:
        """
        if sys.platform == 'darwin':
            return 'influxd'
        elif sys.platform.startswith('win'):
            return os.path.join(self.directory, 'influxdb', 'influxd.exe')
        else:
            return os.path.join(self.directory, 'influxdb', 'usr', 'bin', 'influxd')

    @property
    def log_path(self):
        """
        The path to the general error log for the database.

        :return:
        """
        return os.path.join(self.directory, 'error.log')

    @property
    def influxdb_conf_path(self):
        """
        Path to the configuration file for InfluxDB that specifies ports and enabled plugins.

        :return:
        """
        return os.path.join(self.directory, 'influxdb', 'influxdb.conf')

    @property
    def neo4j_log_path(self):
        """
        The path to the log file to store stdout and stderr for starting and stopping Neo4j instances.

        :return:
        """
        return os.path.join(self.directory, 'neo4j.log')

    @property
    def influxdb_log_path(self):
        """
        The path to the log file to store stdout and stderr for InfluxDB instances.

        :return:
        """
        return os.path.join(self.directory, 'influxdb.log')

    def start(self, timeout=120):
        """
        Function to start the components of a PolyglotDB database.  By the end both the Neo4j database and the InfluxDB
        database will be connectable.  This function blocks until connections are made or until a timeout is reached.

        :return:
        """
        if self.status in ['R']:
            return False
        if self.influxdb_pid is not None or self.neo4j_pid is not None:
            return False
        for k, v in self.ports.items():
            if is_port_in_use(v):
                raise Exception('The port {} is currently in use on this machine.  Please stop any other process'
                                ' using it so that the specified database can be run.'.format(v))
        try:
            with open(self.influxdb_log_path, 'a') as logf:
                influx_proc = subprocess.Popen([self.influxdb_exe_path, '-config', self.influxdb_conf_path],
                                               stdout=logf,
                                               stderr=logf,
                                               stdin=subprocess.DEVNULL,
                                               restore_signals=False,
                                               start_new_session=True)
                self.influxdb_pid = influx_proc.pid
            existing_neo4js = []
            if sys.platform.startswith('win'):
                neo4j_finder = 'WMIC PROCESS get Processid,Caption,Commandline'
            else:
                neo4j_finder = 'ps aux'  # 'ps S'
            proc = subprocess.Popen(neo4j_finder, shell=True,
                                    stdout=subprocess.PIPE)
            stdout, stderr = proc.communicate()
            for line in stdout.decode('utf8').splitlines():
                if 'neo4j' in line and 'java' in line:
                    try:
                        pid = int(line.strip().split()[0])
                    except ValueError:
                        pid = int(line.strip().split()[1])
                    existing_neo4js.append(pid)

            with open(self.neo4j_log_path, 'a') as logf:
                neo4j_proc = subprocess.Popen([self.neo4j_exe_path, 'start'],
                                              stdout=logf,
                                              stderr=logf,
                                              stdin=subprocess.DEVNULL)
                neo4j_proc.communicate()

                neo4j_pid = None
                begin = time.time()
                while neo4j_pid is None:
                    time.sleep(1)
                    if time.time() - begin > timeout:
                        return False
                    proc = subprocess.Popen(neo4j_finder, shell=True,
                                            stdout=subprocess.PIPE)
                    stdout, stderr = proc.communicate()
                    for line in stdout.decode('utf8').splitlines():
                        if 'neo4j' in line and 'java' in line:
                            pid = int(line.strip().split()[1])
                            if pid in existing_neo4js:
                                continue
                            neo4j_pid = pid
                            break
                self.neo4j_pid = neo4j_pid
                begin = time.time()
                while True:
                    time.sleep(1)
                    if time.time() - begin > timeout:
                        raise Exception(
                            'Connection to the Neo4j database could not be established in the specified timeout.')
                    if self.is_running:
                        break
                self.status = 'R'
                self.save()
        except Exception as e:
            with open(self.log_path, 'a') as f:
                exc_type, exc_value, exc_traceback = sys.exc_info()
                traceback.print_exception(exc_type, exc_value, exc_traceback, file=f)
            return False
        return True

    @property
    def ports(self):
        return {'graph_http_port': self.neo4j_http_port, 'graph_bolt_port': self.neo4j_bolt_port,
                'acoustic_http_port': self.influxdb_http_port}

    @property
    def is_running(self):
        """
        Returns are boolean for where the database is currently running (i.e., processing Neo4j Cypher queries).

        :return:
        """
        c = CorpusConfig('')
        c.acoustic_user = None
        c.acoustic_password = None
        # Dockerization: if accessing from the app container, put it up on all interfaces
        # so other containers can connect to it from the app container's IP
        if settings.DOCKER:
            if socket.gethostname() == 'app':
                c.host = '0.0.0.0'
            else:
                c.host = 'app'
        else:
            c.host = 'localhost'
        c.acoustic_http_port = self.influxdb_http_port
        c.graph_user = None
        c.graph_password = None
        c.graph_http_port = self.neo4j_http_port
        c.graph_bolt_port = self.neo4j_bolt_port
        try:
            get_corpora_list(c)
            return True
        except Exception as e:
            print(e)
            return False

    def stop(self):
        """
        Function to stop a PolyglotDB's databases.  This is done through using Neo4j's stop utility and sending an interrupt
        signal to InfluxDB.  This function blocks until both databases are detected to have closed successfully.

        :return:
        """
        if self.neo4j_pid is None:
            raise Exception('Neo4j PID is None')
        if self.status in ['S', 'E']:
            raise Exception('Database is already stopped')
        try:
            os.kill(self.influxdb_pid, signal.SIGINT)
        except ProcessLookupError:
            if os.path.exists(self.log_path):
                with open(self.log_path, 'a') as f:
                    f.write('Could not find influxdb running with PID {}\n'.format(self.influxdb_pid))
        except Exception as e:
            if os.path.exists(self.log_path):
                with open(self.log_path, 'a') as f:
                    exc_type, exc_value, exc_traceback = sys.exc_info()
                    traceback.print_tb(exc_traceback, limit=1, file=f)
        if not os.path.exists(self.neo4j_exe_path):
            pass
        elif os.path.exists(self.neo4j_log_path):
            with open(self.neo4j_log_path, 'a') as logf:
                neo4j_proc = subprocess.Popen([self.neo4j_exe_path, 'stop'],
                                              stdout=logf,
                                              stderr=logf,
                                              stdin=subprocess.DEVNULL)
                neo4j_proc.communicate()

            while True:
                pids = get_pids()
                if self.influxdb_pid not in pids and self.neo4j_pid not in pids:
                    break
        else:
            neo4j_proc = subprocess.Popen([self.neo4j_exe_path, 'stop'],
                                          stdout=subprocess.DEVNULL,
                                          stderr=subprocess.DEVNULL,
                                          stdin=subprocess.DEVNULL)
            neo4j_proc.communicate()

            while True:
                pids = get_pids()
                if self.influxdb_pid not in pids and self.neo4j_pid not in pids:
                    break
        self.influxdb_pid = None
        self.neo4j_pid = None
        self.status = 'S'
        self.save()
        return True

    def install(self):
        """
        Performs the initial set up of a PolyglotDB database.  Vanilla Neo4j and InfluxDB installations are extracted
        (and downloaded if not already cached) and then configured to the specifics of the database.


        """
        shutil.rmtree(self.directory, ignore_errors=True)
        archive_path = download_neo4j()
        extract_neo4j(self.name, archive_path)
        if sys.platform == 'darwin':
            subprocess.call(['brew', 'update'])
            subprocess.call(['brew', 'install', 'influxdb'])
        else:
            archive_path = download_influxdb()
            extract_influxdb(self.name, archive_path)
        self._configure()

    def _configure(self):
        """
        This function performs the configuration for the Neo4j and InfluxDB databases.

        """
        # NEO4J CONFIG
        neo4j_conf_path = os.path.join(self.directory, 'neo4j', 'conf', 'neo4j.conf')
        base_pgdb_dir = os.path.dirname(os.path.abspath(__file__))
        neo4j_template_path = os.path.join(base_pgdb_dir, 'config', 'neo4j.conf')
        with open(neo4j_template_path, 'r') as f:
            template = f.read()

        with open(neo4j_conf_path, 'w') as f:
            f.write(template.format(http_port=self.neo4j_http_port,
                                    https_port=self.neo4j_https_port,
                                    bolt_port=self.neo4j_bolt_port
                                    ))
            # Make remote connections possible
            f.write('\ndbms.connectors.default_listen_address=0.0.0.0')

        # INFLUXDB CONFIG
        influxdb_conf_path = os.path.join(self.directory, 'influxdb', 'influxdb.conf')
        base_pgdb_dir = os.path.dirname(os.path.abspath(__file__))
        influxdb_template_path = os.path.join(base_pgdb_dir, 'config', 'influxdb.conf')
        with open(influxdb_template_path, 'r') as f:
            template = f.read()
        with open(influxdb_conf_path, 'w') as f:
            f.write(template.format(http_port=self.influxdb_http_port,
                                    meta_port=self.influxdb_meta_port,
                                    udp_port=self.influxdb_udp_port,
                                    admin_port=self.influxdb_admin_port,
                                    auth_enabled='false',
                                    data_directory=make_influxdb_safe(
                                        os.path.join(self.directory, 'influxdb', 'data')),
                                    wal_directory=make_influxdb_safe(
                                        os.path.join(self.directory, 'influxdb', 'wal')),
                                    meta_directory=make_influxdb_safe(
                                        os.path.join(self.directory, 'influxdb', 'meta'))))

    def save(self, *args, **kwargs):
        """
        Overwrites the default save method to install the corpus on save (provided it hasn't already been installed)
        """

        used_ports = get_used_ports()
        current_ports = []
        ports = {'neo4j': settings.BASE_NEO4J_PORT, 'influxdb': settings.BASE_INFLUXDB_PORT}
        port_names = ['neo4j_http_port', 'neo4j_https_port', 'neo4j_bolt_port', 'neo4j_admin_port',
                      'influxdb_http_port', 'influxdb_meta_port', 'influxdb_udp_port', 'influxdb_admin_port']
        for p in port_names:
            p_type = 'neo4j'
            if p.startswith('influx'):
                p_type = 'influxdb'
            if not getattr(self, p):
                while True:
                    if ports[p_type] not in used_ports and ports[p_type] not in current_ports:
                        setattr(self, p, ports[p_type])
                        ports[p_type] += 1
                        break
                    ports[p_type] += 1
            current_ports.append(getattr(self, p))

        super(Database, self).save(*args, **kwargs)
        if not os.path.exists(self.directory):
            self.install()

    def delete(self, *args, **kwargs):
        """
        Overwrites the default delete method to ensure the database is stopped and cleaned up from the disc before the
         object is deleted.
        """
        if self.status == 'R':
            self.stop()
        shutil.rmtree(self.directory, ignore_errors=True)
        super(Database, self).delete()


class Corpus(models.Model):
    """
    Corpus objects contain meta data about the PolyglotDB corpora and are the primary interface for running PolyglotDB code.
    """

    class Meta:
        verbose_name_plural = "corpora"
        ordering = ['name']

    MFA = 'M'
    MAUS = 'W'
    FAVE = 'F'
    LABBCAT = 'L'
    PARTITUR = 'P'
    TIMIT = 'T'
    BUCKEYE = 'B'
    FORMAT_CHOICES = (
        (MFA, 'MFA forced aligned TextGrids'),
        (FAVE, 'FAVE forced aligned TextGrids'),
        (MAUS, 'Web-MAUS forced aligned TextGrids'),
        (LABBCAT, 'LaBB-CAT TextGrid output'),
        (PARTITUR, 'Partitur files'),
        (TIMIT, 'TIMIT'),
        (BUCKEYE, 'Buckeye'),
    )

    RESTRICTED = 'R'
    TUTORIAL = 'T'
    PUBLIC = 'P'
    PRIVATE = 'N'
    TYPE_CHOICES = (
        (TUTORIAL, 'Tutorial'),
        (PUBLIC, 'Public'),
        (PRIVATE, 'Private (non-restricted)'),
        (RESTRICTED, 'Restricted'),
    )
    name = models.CharField(max_length=100, unique=True)
    input_format = models.CharField(max_length=1, choices=FORMAT_CHOICES, default=MFA)
    database = models.ForeignKey(Database, on_delete=models.CASCADE, related_name='corpora')
    corpus_type = models.CharField(max_length=1, choices=TYPE_CHOICES, default=RESTRICTED)

    imported = models.BooleanField(default=False)
    busy = models.BooleanField(default=False)
    current_task_id = models.CharField(max_length=250, blank=True, null=True)

    users = models.ManyToManyField(User, through='CorpusPermissions')

    def __str__(self):
        return self.name

    @property
    def config_path(self):
        possible_configs = [os.path.join(self.source_directory, 'config'),
                            os.path.join(self.source_directory, 'config.yaml'), ]
        for p in possible_configs:
            if os.path.exists(p):
                return p
        return None

    @property
    def configuration_data(self):
        path = self.config_path
        conf = {}
        if path is not None and os.path.exists(path):
            with open(path, 'r', encoding='utf8') as f:
                conf = yaml.load(f)
        return conf

    @property
    def syllabics(self):
        conf_data = self.configuration_data
        if "vowel_inventory" in conf_data:
            return conf_data["vowel_inventory"]
        return []

    @property
    def stressed_vowels(self):
        conf_data = self.configuration_data
        if "stressed_vowels" in conf_data:
            return conf_data["stressed_vowels"]
        return []

    @property
    def sibilants(self):
        conf_data = self.configuration_data
        if "sibilant_segments" in conf_data:
            return conf_data["sibilant_segments"]
        return []

    @property
    def import_directory(self):
        tgwav = os.path.join(self.source_directory, 'textgrid-wav')
        if os.path.exists(tgwav):
            return tgwav
        return self.source_directory

    @property
    def source_directory(self):
        return os.path.join(settings.SOURCE_DATA_DIRECTORY, self.name)

    @property
    def enrichment_directory(self):
        return os.path.join(self.source_directory, 'corpus-data', 'enrichment')

    @property
    def data_directory(self):
        """
        Data directory for the corpus, where to store all associated sound files and meta data.
        :return: str
            Corpus's data directory
        """
        return self.database.directory

    @property
    def config(self):
        """
        Generates a :class:`~polyglotdb.CorpusConfig` object for use in connecting to a corpus using the PolyglotDB API.

        :return: :class:`~polyglotdb.CorpusConfig`
            CorpusConfig object to use to connect to the PolyglotDB corpus
        """
        c = CorpusConfig(str(self), data_dir=self.data_directory)
        c.acoustic_user = None
        c.acoustic_password = None
        # Dockerization: if accessing from the app container, put it up on all interfaces
        # so other containers can connect to it from the app container's IP
        if settings.DOCKER:
            if socket.gethostname() == 'app':
                c.host = '0.0.0.0'
            else:
                c.host = 'app'
        else:
            c.host = 'localhost'
        c.acoustic_http_port = self.database.influxdb_http_port
        c.graph_user = None
        c.graph_password = None
        c.graph_http_port = self.database.neo4j_http_port
        c.graph_bolt_port = self.database.neo4j_bolt_port
        return c

    def delete(self, *args, **kwargs):
        """
        Overwrites the default delete method to ensure that corpus information is removed from the PolyglotDB database
        before the object is deleted.

        """
        with CorpusContext(self.config) as c:
            c.reset()
        super(Corpus, self).delete()

    def import_corpus(self):
        """
        Imports a corpus object into the PolyglotDB database using parameters from the object.

        """
        self.imported = False
        self.busy = True
        self.save()
        with CorpusContext(self.config) as c:
            c.reset()
            if self.input_format == self.BUCKEYE:
                parser = pgio.inspect_buckeye(self.source_directory)
            elif self.input_format == self.FAVE:
                parser = pgio.inspect_fave(self.source_directory)
            elif self.input_format == self.MFA:
                parser = pgio.inspect_mfa(self.source_directory)
            elif self.input_format == self.MAUS:
                parser = pgio.inspect_maus(self.source_directory)
            elif self.input_format == self.LABBCAT:
                parser = pgio.inspect_labbcat(self.source_directory)
            elif self.input_format == self.TIMIT:
                parser = pgio.inspect_timit(self.source_directory)
            elif self.input_format == self.PARTITUR:
                parser = pgio.inspect_partitur(self.source_directory)
            else:
                return False
            c.load(parser, self.source_directory)
        self.imported = True
        self.busy = False
        self.save()


    @property
    def has_pauses(self):
        """
        Denotes whether pauses have been encoded for the Corpus
        :return: bool
        """
        with CorpusContext(self.config) as c:
            return c.has_pauses

    @property
    def has_utterances(self):
        """
        Denotes whether utterances have been encoded for the Corpus
        :return: bool
        """
        with CorpusContext(self.config) as c:
            return c.has_utterances

    @property
    def has_syllabics(self):
        """
        Denotes whether syllabic segments have been encoded for the Corpus
        :return: bool
        """
        with CorpusContext(self.config) as c:
            return c.has_syllabics

    @property
    def has_syllables(self):
        """
        Denotes whether syllables have been encoded for the Corpus
        :return: bool
        """
        with CorpusContext(self.config) as c:
            return c.has_syllables


class CorpusPermissions(models.Model):
    corpus = models.ForeignKey(Corpus, on_delete=models.CASCADE, related_name='user_permissions')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='corpus_permissions')

    can_query = models.BooleanField(default=False)
    can_edit = models.BooleanField(default=False)
    can_annotate = models.BooleanField(default=False)
    can_view_annotations = models.BooleanField(default=False)
    can_listen = models.BooleanField(default=False)
    can_view_detail = models.BooleanField(default=False)
    can_enrich = models.BooleanField(default=False)
    can_access_database = models.BooleanField(default=False)
    is_whitelist_exempt = models.BooleanField(default=False)

    def __str__(self):
        output = "Permissions of user '{}' for the '{}' corpus:\n".format(self.user.username, self.corpus.name)
        if self.can_query:
            output += 'Can query\n'
        if self.can_edit:
            output += 'Can edit\n'
        if self.can_annotate:
            output += 'Can annotate\n'
        if self.can_view_annotations:
            output += 'Can view annotations\n'
        if self.can_listen:
            output += 'Can listen\n'
        if self.can_view_detail:
            output += 'Can view detail\n'
        if self.can_enrich:
            output += 'Can enrich\n'
        if self.can_access_database:
            output += 'Can access database\n'
        if self.is_whitelist_exempt:
            output += 'Is whitelist-exempt\n'
        return output

    def set_role_permissions(self):
        # Reset to default state
        self.can_query = False
        self.can_edit = False
        self.can_annotate = False
        self.can_view_annotations = False
        self.can_listen = False
        self.can_view_detail = False
        self.can_enrich = False
        self.can_access_database = False
        self.is_whitelist_exempt = False

        if self.corpus.corpus_type == Corpus.PUBLIC:  # Public corpora
            # Perms for everyone
            self.can_query = True
            self.can_listen = True
            self.can_view_detail = True
            if self.user.profile.user_type == Profile.ANNOTATOR: # Annotators
                self.can_annotate = True
            if self.user.profile.user_type in [Profile.RESEARCHER, Profile.UNLIMITED]:
                self.can_view_annotations = True
                self.can_edit = True
                self.can_enrich = True
                self.can_access_database = True
        elif self.corpus.corpus_type == Corpus.TUTORIAL:  # Tutorial corpora
            if self.user.profile.user_type == Profile.UNLIMITED or self.corpus.name.split('-')[-1] == self.user.username:
                self.can_query = True
                self.can_listen = True
                self.can_view_detail = True
                self.can_annotate = True
                self.can_view_annotations = True
                self.can_edit = True
                self.can_enrich = True
                self.can_access_database = True
        elif self.corpus.corpus_type in [Corpus.RESTRICTED, Corpus.PRIVATE]:  # Private/restricted corpora
            if self.corpus.corpus_type == Corpus.PRIVATE:  # Private (non-restricted) corpora
                self.is_whitelist_exempt = True
            if self.user.profile.user_type in [Profile.RESEARCHER, Profile.UNLIMITED]:
                self.can_query = True
                self.can_enrich = True
                self.can_access_database = True
                if self.user.profile.user_type == Profile.UNLIMITED:
                    self.can_edit = True
                    self.can_listen = True
                    self.can_view_detail = True
                    self.can_annotate = True
                    self.can_view_annotations = True


@receiver(post_save, sender=Corpus)
def update_user_permissions(sender, instance, created, **kwargs):
    if created:
        new_permissions = []
        for u in User.objects.select_related('profile').all():
                perm = CorpusPermissions(corpus=instance, user=u)
                perm.set_role_permissions()
                new_permissions.append(perm)
        if new_permissions:
            CorpusPermissions.objects.bulk_create(new_permissions)


class Enrichment(models.Model):
    name = models.CharField(max_length=100)
    corpus = models.ForeignKey(Corpus, on_delete=models.CASCADE)
    running = models.BooleanField(default=False)
    completed = models.BooleanField(default=False)
    last_run = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return '{} - {}'.format(self.corpus.name, self.name)

    @property
    def config(self):
        with open(self.config_path, 'r') as f:
            config = json.load(f)
        return config

    @config.setter
    def config(self, new_config):
        with open(self.config_path, 'w') as f:
            json.dump(new_config, f)

    @property
    def directory(self):
        directory = os.path.join(settings.POLYGLOT_ENRICHMENT_DIRECTORY, str(self.pk))
        os.makedirs(directory, exist_ok=True)
        return directory

    @property
    def config_path(self):
        return os.path.join(self.directory, 'config.json')

    @property
    def results_path(self):
        return os.path.join(self.directory, 'results.txt')

    @property
    def runnable(self):
        config = self.config
        enrichment_type = config.get('enrichment_type')
        with CorpusContext(self.corpus.config) as c:
            if enrichment_type == 'subset':
                annotation_type = config.get('annotation_type')
                if not (annotation_type in c.hierarchy.annotation_types):
                    return 'Must encode {}'.format(annotation_type)
                if c.hierarchy.has_token_subset(annotation_type, config.get('subset_label', '')) or \
                        c.hierarchy.has_type_subset(annotation_type, config.get('subset_label', '')):
                    return "The {} subset already exists".format(config.get('subset_label', ''))
            elif enrichment_type == 'syllables':
                if not (c.hierarchy.has_type_subset('phone', config.get('phone_class', 'syllabic'))):
                    return 'Must encode {}'.format(config.get('phone_class', 'syllabic'))
            elif enrichment_type == 'refined_formant_points':
                if not (c.hierarchy.has_type_subset('phone', config.get('phone_class', 'vowel'))):
                    return 'Must encode {}'.format(config.get('phone_class', 'vowel'))
            elif enrichment_type == 'utterances':
                if not (c.hierarchy.has_token_subset('word', 'pause')):
                    return 'Must encode pauses'
            elif enrichment_type == 'vot':
                #Check if utterances
                if not "utterance" in c.hierarchy.annotation_types:
                    return 'Must encode utterances'
            elif 'csv' in enrichment_type:
                if config.get('path') is None:
                    return 'Must attach a file'
                if enrichment_type == "importcsv":
                    if not config.get("id_column", ''):
                        return "One of the columns must have an ID"
                    elif not config.get("annotation_type") in ["phone", "utterance", "syllable", "word"]:
                        return "{} is not an annotation type".format(config.get('annotation_type'))
            elif enrichment_type == 'hierarchical_property':
                higher_annotation = config.get('higher_annotation')
                lower_annotation = config.get('lower_annotation')
                if not (
                        higher_annotation in c.hierarchy.annotation_types and lower_annotation in c.hierarchy.annotation_types):
                    return 'Must encode {} and {}'.format(higher_annotation, lower_annotation)
        return 'runnable'

    def reset_enrichment(self):
        self.running = True
        self.save()
        self.corpus.busy = True
        self.corpus.save()
        config = self.config
        enrichment_type = config.get('enrichment_type')
        try:
            with CorpusContext(self.corpus.config) as c:
                if enrichment_type == 'subset':
                    annotation_type = config.get('annotation_type')
                    subset_label = config.get('subset_label')
                    c.reset_type_subset(annotation_type, subset_label)
                elif enrichment_type == 'syllables':
                    c.reset_syllables()
                elif enrichment_type == 'pauses':
                    c.reset_pauses()
                elif enrichment_type == 'utterances':
                    c.reset_utterances()
                elif enrichment_type == 'hierarchical_property':
                    property_type = config.get('property_type')
                    higher_annotation = config.get('higher_annotation')
                    lower_annotation = config.get('lower_annotation')
                    property_label = config.get('property_label')
                    if property_type == 'rate':
                        c.reset_property(higher_annotation, property_label)
                    elif property_type == 'count':
                        c.reset_property(higher_annotation, property_label)
                    elif property_type == 'position':
                        c.reset_property(lower_annotation, property_label)
                elif enrichment_type == 'importcsv':
                    columns = [x["name"] for x in config.get('columns') if x["included"]]
                    for col in columns:
                        c.reset_property(config.get('annotation_type'), col)
                elif enrichment_type == 'pitch':
                    c.reset_acoustic_measure('pitch')
                elif enrichment_type == 'relativize_pitch':
                    c.reset_relativized_acoustic_measure('pitch')
                elif enrichment_type == 'discourse_csv':
                    c.reset_discourse_csv(config.get('path'))
                elif enrichment_type == 'phone_csv':
                    c.reset_inventory_csv(config.get('path'))
                elif enrichment_type == 'speaker_csv':
                    c.reset_speaker_csv(config.get('path'))
                elif enrichment_type == 'lexicon_csv':
                    c.reset_lexicon_csv(config.get('path'))
                elif enrichment_type == 'formants':
                    c.reset_acoustic_measure('formants')
                elif enrichment_type == 'refined_formant_points':
                    if config.get("output_tracks", False):
                        c.reset_acoustic_measure('formants')
                    else:
                        c.reset_formant_points()
                elif enrichment_type == 'intensity':
                    c.reset_acoustic_measure('intensity')
                elif enrichment_type == 'vot':
                    c.reset_vot()
                elif enrichment_type == 'relativize_property':
                    annotation_type = config.get('annotation_type')
                    property_name = 'relativized_' + config.get('property_name')
                    by_speaker = config.get('by_speaker')
                    if by_speaker:
                        property_name += '_by_speaker'
                    c.reset_property(annotation_type, property_name)
                elif enrichment_type == 'relativize_intensity':
                    c.reset_relativized_acoustic_measure('intensity')
                elif enrichment_type == 'relativize_formants':
                    c.reset_relativized_acoustic_measure('formants')
                elif enrichment_type == 'patterned_stress':
                    q = c.query_graph(c.syllable).set_properties(stress=None)
                elif enrichment_type == 'praat_script':
                    props = config.get('properties', ['cog', 'slope', 'spread', 'peak'])
                    annotation_type = config.get('annotation_type', 'phone')
                    q = c.query_graph(getattr(c, annotation_type)) \
                            .filter(getattr(c, annotation_type).config.get.subset == config.get('subset'))
                    q.set_properties(**{x: None for x in props})
            self.running = False
            self.completed = False
            self.last_run = None
            self.save()
            self.corpus.busy = False
            self.corpus.save()
        except Exception:
            self.corpus.busy = False  # If it fails, don't stay busy and block everything
            self.corpus.save()
            self.running = False
            print(traceback.format_exc())

    def run_enrichment(self):
        self.running = True
        self.save()
        self.corpus.busy = True
        self.corpus.save()
        config = self.config
        enrichment_type = config.get('enrichment_type')
        try:
            with CorpusContext(self.corpus.config) as c:
                if enrichment_type == 'subset':
                    annotation_type = config.get('annotation_type')
                    annotation_labels = config.get('annotation_labels')
                    subset_label = config.get('subset_label')
                    c.encode_type_subset(annotation_type, annotation_labels, subset_label)
                elif enrichment_type == 'syllables':
                    syllabic_label = config.get('phone_class', 'syllabic')
                    if c.hierarchy.has_type_subset('phone', syllabic_label):
                        algorithm = config.get('algorithm', 'maxonset')
                        c.encode_syllables(algorithm, syllabic_label=syllabic_label)
                elif enrichment_type == 'pauses':
                    pause_label = config.get('pause_label')
                    c.encode_pauses(pause_label)
                elif enrichment_type == 'utterances':
                    pause_length = float(config.get('pause_length', 150)) / 1000
                    c.encode_utterances(min_pause_length=pause_length)
                elif enrichment_type == 'hierarchical_property':
                    property_type = config.get('property_type')
                    higher_annotation = config.get('higher_annotation')
                    lower_annotation = config.get('lower_annotation')
                    property_label = config.get('property_label').replace(" ", "_")
                    subset_label = config.get('subset_label', '')
                    if not subset_label:
                        subset_label = None
                    if property_type == 'rate':
                        c.encode_rate(higher_annotation, lower_annotation, property_label, subset=subset_label)
                    elif property_type == 'count':
                        c.encode_count(higher_annotation, lower_annotation, property_label, subset=subset_label)
                    elif property_type == 'position':
                        c.encode_position(higher_annotation, lower_annotation, property_label, subset=subset_label)
                elif enrichment_type == 'discourse_csv':
                    c.enrich_discourses_from_csv(config.get('path'))
                elif enrichment_type == 'phone_csv':
                    c.enrich_inventory_from_csv(config.get('path'))
                elif enrichment_type == 'speaker_csv':
                    c.enrich_speakers_from_csv(config.get('path'))
                elif enrichment_type == 'lexicon_csv':
                    c.enrich_lexicon_from_csv(config.get('path'))
                elif enrichment_type == 'pitch':
                    c.analyze_pitch(source=config.get('source', 'praat'), multiprocessing=False)
                elif enrichment_type == 'formants':
                    c.analyze_formant_tracks(source=config.get('source', 'praat'), multiprocessing=False)
                elif enrichment_type == 'refined_formant_points':
                    from polyglotdb.acoustics.formants.refined import analyze_formant_points_refinement
                    duration_threshold = float(config.get('duration_threshold', 0.0)) / 1000
                    nIterations = int(config.get('number_of_iterations'))
                    vowel_prototypes_path = config.get('path', None)
                    output_tracks = config.get('output_tracks', False)
                    vowel_label = config.get('phone_class', 'vowel')
                    metadata = analyze_formant_points_refinement(c, duration_threshold=duration_threshold,
                                                                 num_iterations=nIterations,
                                                                 vowel_label=vowel_label,
                                                                 vowel_prototypes_path=vowel_prototypes_path,
                                                                 output_tracks=output_tracks,
                                                                 multiprocessing=False
                                                                 )
                elif enrichment_type == 'intensity':
                    c.analyze_intensity(source=config.get('source', 'praat'), multiprocessing=False)
                elif enrichment_type == 'relativize_property':
                    annotation_type = config.get('annotation_type')
                    property_name = config.get('property_name')
                    by_speaker = config.get('by_speaker')
                    c.encode_relativized(annotation_type, property_name, by_speaker)
                elif enrichment_type == 'relativize_pitch':
                    c.relativize_pitch(by_speaker=config.get('by_speaker', True), by_phone=config.get('by_phone', True))
                elif enrichment_type == 'relativize_intensity':
                    c.relativize_intensity(by_speaker=config.get('by_speaker', True))
                elif enrichment_type == 'relativize_formants':
                    c.relativize_formants(by_speaker=config.get('by_speaker', True), by_phone=config.get('by_phone', True))
                elif enrichment_type == 'praat_script':
                    properties = c.analyze_script(annotation_type=config.get('annotation_type', 'phone'),
                                                      subset=config.get('subset'), 
                                                      script_path=config.get('path'),
                                                      multiprocessing=False)
                    config['properties'] = properties
                    self.config = config
                elif enrichment_type == 'patterned_stress':
                    c.encode_stress_from_word_property(config.get('word_property'))
                elif enrichment_type == 'importcsv':
                    columns = config.get('columns') 
                    c.enrich_tokens_with_csv(config.get('path'), config.get('annotation_type'), 
                            config.get('id_column'), properties=[x['name'] for x in columns if x['included']])
                elif enrichment_type == 'vot':
                    c.analyze_vot(stop_label=config.get('stop_label'),
                            classifier=config.get('classifier', '/site/proj/PolyglotDB/tests/data/classifier/sotc_classifiers/sotc_voiceless.classifier'),
                            vot_min=int(config.get('vot_min')),
                            vot_max=int(config.get('vot_max')),
                            overwrite_edited=config.get('overwrite_edited'),
                            window_min=int(config.get('window_min')),
                            window_max=int(config.get('window_max')))
            self.running = False
            self.completed = True
            self.last_run = datetime.datetime.now()
            self.save()
            self.corpus.busy = False
            self.corpus.save()
        except Exception:
            self.corpus.busy = False  # If it fails, don't stay busy and block everything
            self.corpus.save()
            self.running = False
            self.completed = False
            print(traceback.format_exc())

class BackgroundTask(models.Model):
    task_id = models.UUIDField(primary_key=True)
    name = models.CharField(max_length=100)

    #Allow optional corpus for script tasks
    corpus = models.ForeignKey(Corpus, null=True, blank=True, on_delete=models.CASCADE)
    running = models.BooleanField(default=True)
    failed = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name_plural = 'Background Tasks'

    def get_exceptions(self):
        result = AsyncResult(self.task_id)
        return result.result

    def status(self):
        return AsyncResult(self.task_id).status()

class SpadeScript(models.Model):
    task = models.ForeignKey(BackgroundTask, on_delete=models.CASCADE)
    corpus_name = models.CharField(max_length=100)
    script_name = models.CharField(max_length=100)
    reset = models.BooleanField(default=False)

    class Meta:
        verbose_name_plural = 'Spade Scripts'

    @property
    def log_path(self):
        if not os.path.isdir(settings.POLYGLOT_SCRIPT_DIRECTORY):
            os.makedirs(settings.POLYGLOT_SCRIPT_DIRECTORY, exist_ok=True)
        return os.path.join(settings.POLYGLOT_SCRIPT_DIRECTORY, str(self.pk))

    def run_script(self):
        run_spade_script(self.script_name, self.corpus_name, self.reset, self.log_path)





class Query(models.Model):
    ANNOTATION_TYPE_CHOICES = (('U', 'Utterance'),
                               ('W', 'Word'),
                               ('S', 'Syllable'),
                               ('P', 'Phone'))
    name = models.CharField(max_length=100)
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    annotation_type = models.CharField(max_length=1, choices=ANNOTATION_TYPE_CHOICES)
    corpus = models.ForeignKey(Corpus, on_delete=models.CASCADE)
    running = models.BooleanField(default=False)
    result_count = models.IntegerField(null=True, blank=True)

    class Meta:
        verbose_name_plural = 'Queries'

    def __str__(self):
        return '{} - {}'.format(self.corpus.name, self.name)

    @property
    def config(self):
        with open(self.config_path, 'r') as f:
            config = json.load(f)
        return config

    @config.setter
    def config(self, new_config):
        with open(self.config_path, 'w') as f:
            json.dump(new_config, f)

    def resort(self, ordering):
        if not hasattr(self, '_results'):
            self._results = None
            self._count = 0
            if os.path.exists(self.results_path):
                with open(self.results_path, 'r') as f:
                    self._results = json.load(f)
                self._count = len(self._results)
            else:
                self.run_query()
        self._ordering = self.config.get('ordering', None)
        if ordering != self._ordering and ordering:
            ordering = ordering.replace(self.annotation_type.lower() + '.', '')
            self._ordering = ordering

            def order_function(input):
                ordering = self._ordering.replace('-', '').split('.')
                item = input
                for o in ordering:
                    item = item[o]
                return item

            self._results.sort(key=order_function, reverse=self._ordering.startswith('-'))
            with open(self.results_path, 'w') as f:
                json.dump(self._results, f)

    def get_results(self, ordering, limit, offset):
        if self.running:
            return None
        if not hasattr(self, '_results'):
            self._results = None
            self._count = 0
            if os.path.exists(self.results_path):
                with open(self.results_path, 'r') as f:
                    self._results = json.load(f)
                self._count = len(self._results)
            else:
                self.run_query()
        if self._results is None:
            return None
        self._ordering = self.config.get('ordering', None)
        if ordering != self._ordering and ordering:
            with CorpusContext(self.corpus.config) as c:
                self._ordering = ordering
                ordering = self._ordering.replace('-', '').split('.')
                att = None
                for o in ordering:
                    if att is None:
                        att = getattr(c, o)
                    else:
                        att = getattr(att, o)

                def order_function(input):
                    item = input
                    for o in ordering:
                        if isinstance(item, list):
                            if len(item):
                                item = item[0]
                            else:
                                return att.value_type()()
                        item = item[o]
                        if item is None:
                            return att.value_type()()
                    return item

                self._results.sort(key=order_function, reverse=self._ordering.startswith('-'))
        if limit is None or limit == 0:
            res = self._results[offset:]
        else:
            res = self._results[offset:offset + limit]
        ind = offset
        for r in res:
            r['index'] = ind
            ind += 1
        return res

    def generate_filter(self, att, value, operator):
        if operator == '==':
            filter = att == value
        elif operator == '!=':
            filter = att != value
        elif operator == '>':
            filter = att > value
        elif operator == '>=':
            filter = att >= value
        elif operator == '<':
            filter = att < value
        elif operator == '<=':
            filter = att <= value
        elif operator == 'in':
            filter = att.in_(value)
        elif operator == 'not in':
            filter = att.not_in_(value)
        else:
            raise Exception('Invalid operator "{}"'.format(operator))
        return filter

    def generate_query_for_export(self, corpus_context):
        a_type = self.get_annotation_type_display().lower()
        config = self.config
        a = getattr(corpus_context, a_type)
        acoustic_columns = config.get('acoustic_columns', {})
        acoustic_tracks = config.get('acoustic_tracks', {})
        columns = config.get('columns', {})
        column_names = config.get('column_names', {})
        q = self.generate_base_query(corpus_context)
        for f_a_type, position_columns in columns.items():
            for position, a_columns in position_columns.items():
                if not a_columns:
                    continue
                if f_a_type in {'speaker', 'discourse'}:
                    field, val = position, a_columns
                    if not val:
                        continue
                    ann = getattr(a, f_a_type)
                    att = getattr(ann, field)
                    try:
                        att = att.column_name(column_names[f_a_type][position])
                    except KeyError:
                        pass
                    q = q.columns(att)
                elif f_a_type in corpus_context.hierarchy.annotation_types:
                    for field, val in a_columns.items():
                        if not val:
                            continue
                        if f_a_type == a_type:
                            ann = a
                        else:
                            ann = getattr(a, f_a_type)
                        if position != 'current':
                            ps = position.split('_')
                            for p in ps:
                                ann = getattr(ann, p)
                        if field != 'subannotations':
                            att = getattr(ann, field)
                            try:
                                att = att.column_name(column_names[f_a_type][position][field])
                            except KeyError:
                                pass
                            q = q.columns(att)
                        else:
                            for s_name, s_columns in val.items():
                                for s_field, s_val in s_columns.items():
                                    if not s_val:
                                        continue
                                    att = getattr(getattr(ann, s_name), s_field)
                                    try:
                                        att = att.column_name(
                                            column_names[f_a_type][position]['subannotations'][s_name][s_field])
                                    except KeyError:
                                        pass
                                    q = q.columns(att)

        print(acoustic_columns)
        for a_column, props in acoustic_columns.items():
            if a_column not in corpus_context.hierarchy.acoustics:
                continue
            relative_aggregate = props.pop('relative', False)
            acoustic = getattr(a, a_column)
            acoustic.relative = relative_aggregate
            for c, v in props.items():
                if not v:
                    continue
                if c not in ['mean', 'stdev', 'median', 'max', 'min']:
                    continue
                q = q.columns(getattr(acoustic, c))

        print(acoustic_tracks)
        for a_column, props in acoustic_tracks.items():
            # track props
            include_track = props.pop('include', False)
            if not include_track:
                continue
            relative_time = props.pop('relative_time', False)
            relative_track = props.pop('relative_track', False)
            num_points = props.pop('num_points', '')

            if include_track:
                acoustic = getattr(a, a_column)
                acoustic.relative_time = relative_time
                acoustic.relative = relative_track
                try:
                    num_points = int(num_points)
                    acoustic = acoustic.interpolated_track
                    acoustic.num_points = num_points
                except ValueError:
                    acoustic = acoustic.track
                q = q.columns(acoustic)
        return q

    def generate_base_query(self, corpus_context):
        a_type = self.get_annotation_type_display().lower()
        config = self.config

        a = getattr(corpus_context, a_type)
        q = corpus_context.query_graph(a)
        for f_a_type, positions in config['filters'].items():
            if f_a_type not in {'speaker', 'discourse'} | set(corpus_context.hierarchy.annotation_types):
                continue
            if f_a_type == a_type:
                ann = a
            else:
                ann = getattr(a, f_a_type)
            if f_a_type in {'speaker', 'discourse'}:
                a_filters = positions
                if a_filters:
                    if isinstance(a_filters, dict):
                        for field, value in a_filters.items():
                            att = getattr(ann, field)
                            if value == 'null':
                                value = None
                            else:
                                value = att.coerce_value(value)
                            if value is None:
                                continue
                            operator = a_filters.get('operator', '==')
                            filter = self.generate_filter(att, value, operator)
                            q = q.filter(filter)
                    else:
                        for d in a_filters:
                            field, value, operator = d['property'], d['value'], d.get('operator', '==')
                            text = d.get('searchText', '')
                            if value is None and text:
                                value = text
                            att = getattr(ann, field)
                            if value == 'null':
                                value = None
                            else:
                                value = att.coerce_value(value)
                            if value is None:
                                continue
                            filter = self.generate_filter(att, value, operator)
                            q = q.filter(filter)
            else:
                for position, filter_types in positions.items():
                    if f_a_type == a_type:
                        ann = a
                    else:
                        ann = getattr(a, f_a_type)
                    if f_a_type == a_type:
                        current_ann = a
                    else:
                        current_ann = getattr(a, f_a_type)
                    if position != 'current':
                        position = position.split('_')
                        for p in position:
                            ann = getattr(ann, p)
                    a_filters = filter_types.get('property_filters', [])
                    if a_filters:
                        if isinstance(a_filters, dict):
                            for field, value in a_filters.items():
                                att = getattr(ann, field)
                                if value == 'null':
                                    value = None
                                else:
                                    value = att.coerce_value(value)
                                if value is None:
                                    continue
                                att = getattr(ann, field)
                                q = q.filter(att == value)
                        else:
                            for d in a_filters:
                                field, value, operator = d['property'], d['value'], d.get('operator', '==')
                                text = d.get('searchText', '')
                                if value is None and text:
                                    value = text
                                att = getattr(ann, field)
                                if value == 'null':
                                    value = None
                                else:
                                    value = att.coerce_value(value)
                                if value is None:
                                    continue
                                filter = self.generate_filter(att, value, operator)
                                q = q.filter(filter)
                    subset_filters = filter_types.get('subset_filters', [])

                    for s in subset_filters:
                        q = q.filter(ann.subset == s)

                    inverse_subset_filters = filter_types.get('inverse_subset_filters', [])

                    for s in inverse_subset_filters:
                        q = q.filter(ann.subset != s)

                    subannotation_filters = filter_types.get('subannotation_filters', {})
                    if not subannotation_filters:
                        subannotation_filters = {}
                    for s_type, a_filters in subannotation_filters.items():
                        s_ann = getattr(ann, s_type)
                        if not a_filters:
                            continue
                        if isinstance(a_filters, dict):
                            for field, value in a_filters.items():
                                att = getattr(s_ann, field)
                                if value == 'null':
                                    value = None
                                else:
                                    value = att.coerce_value(value)
                                if value is None:
                                    continue
                                att = getattr(s_ann, field)
                                q = q.filter(att == value)
                        else:
                            for d in a_filters:
                                field, value, operator = d['property'], d['value'], d.get('operator', '==')
                                att = getattr(s_ann, field)
                                if value == 'null':
                                    value = None
                                else:
                                    value = att.coerce_value(value)
                                if value is None:
                                    continue
                                filter = self.generate_filter(att, value, operator)
                                q = q.filter(filter)

                    left_aligned_filter = filter_types.get('left_aligned_filter', '')
                    right_aligned_filter = filter_types.get('right_aligned_filter', '')
                    if left_aligned_filter and left_aligned_filter != f_a_type:
                        q = q.filter(
                            getattr(ann, 'begin') == getattr(getattr(current_ann, left_aligned_filter), 'begin'))
                    if right_aligned_filter and left_aligned_filter != f_a_type:
                        q = q.filter(getattr(ann, 'end') == getattr(getattr(current_ann, right_aligned_filter), 'end'))
        return q

    def run_query(self):
        self.running = True
        self.result_count = None
        self.save()
        from .serializers import serializer_factory
        while os.path.exists(self.lockfile_path):
            pass
        with open(self.lockfile_path, 'w') as f:
            pass
        if os.path.exists(self.results_path):
            os.remove(self.results_path)
        try:
            a_type = self.get_annotation_type_display().lower()
            config = self.config
            acoustic_columns = config.get('acoustic_columns', {})
            cache_acoustics = config.get('cache_acoustics', False)
            with CorpusContext(self.corpus.config) as c:
                a = getattr(c, a_type)
                q = self.generate_base_query(c)
                self._count = q.count()
                q = q.preload(getattr(a, 'discourse'), getattr(a, 'speaker'))
                acoustic_column_names = []
                if cache_acoustics:
                    for a_column, props in acoustic_columns.items():
                        # track props
                        include_track = props.pop('include', False)
                        relative_time = props.pop('relative_time', False)
                        relative_track = props.pop('relative_track', False)
                        num_points = props.pop('num_points', '')
                        if include_track:
                            acoustic = getattr(a, a_column)
                            acoustic.relative_time = relative_time
                            acoustic.relative = relative_track
                            try:
                                num_points = int(num_points)
                                acoustic = acoustic.interpolated_track
                                acoustic.num_points = num_points
                            except ValueError:
                                acoustic = acoustic.track
                            q = q.preload_acoustics(acoustic)
                            acoustic_column_names.append(a_column)
                for t in c.hierarchy.annotation_types:
                    if t in c.hierarchy.subannotations:
                        for s in c.hierarchy.subannotations[t]:
                            if t == a_type:
                                q = q.preload(getattr(a, s))
                            else:
                                q = q.preload(getattr(getattr(a, t), s))
                for t in c.hierarchy.get_higher_types(a_type):
                    q = q.preload(getattr(a, t))
                positions = config['positions']
                for f_a_type, pos in positions.items():
                    for position in pos:
                        if position == 'current':
                            continue
                        if f_a_type == a_type:
                            ann = a
                        else:
                            ann = getattr(a, f_a_type)
                        position = position.split('_')
                        for p in position:
                            ann = getattr(ann, p)
                        q = q.preload(ann)
                res = q.all()
                serializer_class = serializer_factory(c.hierarchy, a_type, positions=positions, top_level=True,
                                                      acoustic_columns=acoustic_column_names, detail=False,
                                                      with_higher_annotations=True,
                                                      with_subannotations=True)
                serializer = serializer_class(res, many=True)

                self._results = serializer.data
                ordering = self.config.get('ordering', None)
                if ordering:
                    ordering = ordering.replace(self.annotation_type.lower() + '.', '')
                    self._ordering = ordering

                    def order_function(input):
                        ordering = self._ordering.replace('-', '').split('.')
                        item = input
                        for o in ordering:
                            if isinstance(item, list):
                                item = item[0]
                            item = item[o]
                        return item

                    self._results.sort(key=order_function, reverse=self._ordering.startswith('-'))
                with open(self.results_path, 'w') as f:
                    json.dump(self._results, f)

                self.result_count = len(self._results)
        except:
            raise
        finally:
            os.remove(self.lockfile_path)
            self.running = False
            self.save()

    def generate_subset(self):
        self.running = True
        config = self.config
        config['subset_encoded'] = False
        self.config = config
        self.save()
        while os.path.exists(self.lockfile_path):
            pass
        with open(self.lockfile_path, 'w') as f:
            pass
        try:
            begin = time.time()
            with CorpusContext(self.corpus.config) as c:
                q = self.generate_query_for_export(c)
                q.create_subset(config["subset_name"])
                c.encode_hierarchy()
            config = self.config
            config['subset_encoded'] = True
            self.config = config
        except:
            raise
        finally:
            os.remove(self.lockfile_path)
            self.running = False
            self.save()

    def export_query(self):
        self.running = True
        config = self.config
        config['export_available'] = False
        self.config = config
        self.save()
        while os.path.exists(self.lockfile_path):
            pass
        with open(self.lockfile_path, 'w') as f:
            pass
        try:
            with CorpusContext(self.corpus.config) as c, open(self.export_path, 'w', newline='', encoding='utf8') as f:
                q = self.generate_query_for_export(c)
                writer = csv.writer(f)
                q.to_csv(writer)
            config = self.config
            config['export_available'] = True
            self.config = config
        except:
            raise
        finally:
            os.remove(self.lockfile_path)
            self.running = False
            self.save()

    def save(self, force_insert=False, force_update=False, using=None,
             update_fields=None):
        super(Query, self).save(force_insert, force_update, using, update_fields)
        os.makedirs(self.directory, exist_ok=True)

    def delete(self, using=None, keep_parents=False):
        shutil.rmtree(self.directory, ignore_errors=True)
        super(Query, self).delete(using, keep_parents)

    @property
    def directory(self):
        directory = os.path.join(settings.POLYGLOT_QUERY_DIRECTORY, str(self.pk))
        os.makedirs(directory, exist_ok=True)
        return directory

    @property
    def lockfile_path(self):
        return os.path.join(self.directory, 'lockfile')

    @property
    def config_path(self):
        return os.path.join(self.directory, 'config.json')

    @property
    def results_path(self):
        return os.path.join(self.directory, 'results.txt')

    @property
    def export_path(self):
        return os.path.join(self.directory, '{}_export.csv'.format(self.name))

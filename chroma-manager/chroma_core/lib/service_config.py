#
# INTEL CONFIDENTIAL
#
# Copyright 2013 Intel Corporation All Rights Reserved.
#
# The source code contained or described herein and all documents related
# to the source code ("Material") are owned by Intel Corporation or its
# suppliers or licensors. Title to the Material remains with Intel Corporation
# or its suppliers and licensors. The Material contains trade secrets and
# proprietary and confidential information of Intel or its suppliers and
# licensors. The Material is protected by worldwide copyright and trade secret
# laws and treaty provisions. No part of the Material may be used, copied,
# reproduced, modified, published, uploaded, posted, transmitted, distributed,
# or disclosed in any way without Intel's prior express written permission.
#
# No license under any patent, copyright, trade secret or other intellectual
# property right is granted to or conferred upon you by disclosure or delivery
# of the Materials, either expressly, by implication, inducement, estoppel or
# otherwise. Any license under such intellectual property rights must be
# express and approved by Intel in writing.


import hashlib
import logging
import getpass
import socket
import sys
import xmlrpclib
import time
import os
import json
import tempfile

from chroma_core.lib.util import chroma_settings, CommandLine, CommandError


log = logging.getLogger('installation')
log.addHandler(logging.StreamHandler())
log.setLevel(logging.INFO)

settings = chroma_settings()

from django.contrib.auth.models import User, Group
from django.core.management import ManagementUtility

from chroma_core.services.http_agent.crypto import Crypto

from supervisor.xmlrpc import SupervisorTransport

from chroma_core.models.bundle import Bundle
from chroma_core.models.server_profile import ServerProfile, ServerProfilePackage


class SupervisorStatus(object):
    def __init__(self):
        username = None
        password = None

        if settings.DEBUG:
            # In development, use inet_http_server set up by django-supervisor
            username = hashlib.md5(settings.SECRET_KEY).hexdigest()[:7]
            password = hashlib.md5(username).hexdigest()

            url = "http://localhost:9100/RPC2"
        else:
            # In production, use static inet_http_server settings
            url = "http://localhost:9100/RPC2"

        self._xmlrpc = xmlrpclib.ServerProxy(
            'http://127.0.0.1',
            transport = SupervisorTransport(username,
                                            password,
                                            url)
        )

    def get_all_process_info(self):
        return self._xmlrpc.supervisor.getAllProcessInfo()

    def get_non_running_services(self):
        return [p['name'] for p in SupervisorStatus().get_all_process_info() if p['statename'] != 'RUNNING']


class NTPConfig:
    CONFIG_FILE = "/etc/ntp.conf"
    SENTINEL = "# Added by chroma-manager\n"
    COMMENTED = "# Commented by chroma-manager: "

    def __init__(self, config_file = None):
        self.config_file = config_file or self.CONFIG_FILE

    def open_conf_for_edit(self):
        tmp_f, tmp_name = tempfile.mkstemp(dir = '/etc')
        f = open('/etc/ntp.conf', 'r')
        return tmp_f, tmp_name, f

    def close_conf(self, tmp_f, tmp_name, f):
        f.close()
        os.close(tmp_f)
        if not os.path.exists("/etc/ntp.conf.pre-chroma"):
            os.rename("/etc/ntp.conf", "/etc/ntp.conf.pre-chroma")
        os.chmod(tmp_name, 0644)
        os.rename(tmp_name, "/etc/ntp.conf")

    def remove(self):
        """Remove our config section from the ntp config file, or do
        nothing if our section is not there"""
        tmp_f, tmp_name, f = self.open_conf_for_edit()
        skip = False
        for line in f.readlines():
            if skip:
                if line == self.SENTINEL:
                    skip = False
                continue
            if line == self.SENTINEL:
                skip = True
                continue
            if line.startswith(self.COMMENTED):
                line = line[len(self.COMMENTED):]
            os.write(tmp_f, line)
        self.close_conf(tmp_f, tmp_name, f)

    def add(self, server):
        tmp_f, tmp_name, f = self.open_conf_for_edit()
        added_server = False
        for line in f.readlines():
            if line.startswith("server "):
                line = "%s%s" % (self.COMMENTED, line)
                if server != "localhost" and not added_server:
                    line = "%sserver %s\n%s%s" % (self.SENTINEL, server, self.SENTINEL, line)
                    added_server = True
            if server == "localhost" and line.startswith("#fudge"):
                line = "%s%sserver  127.127.1.0     # local clock\nfudge   127.127.1.0 stratum 10\n%s" % (line, self.SENTINEL, self.SENTINEL)
            os.write(tmp_f, line)
        self.close_conf(tmp_f, tmp_name, f)


class ServiceConfig(CommandLine):
    def __init__(self):
        self.verbose = False

    def _check_name_resolution(self):
        """
        Check:
         * that the hostname is not localhost
         * that the FQDN can be looked up from hostname
         * that an IP can be looked up from the hostname

        This check is done ahead of configuring RabbitMQ, which fails unobviously if the
        name resolution is bad.

        :return: True if OK, else False

        """
        try:
            hostname = socket.gethostname()
        except socket.error:
            log.error("Error: Unable to get the servers hostname. Please correct the hostname resolution.")
            return False

        if hostname == "localhost":
            log.error("Error: Currently the hostname is '%s' which is invalid. "
                      "Please correct the hostname resolution.", hostname)
            return False

        try:
            fqdn = socket.getfqdn(hostname)
        except socket.error:
            log.error("Error: Unable to get the FQDN for the server name '%s'. "
                      "Please correct the hostname resolution.", hostname)
            return False
        else:
            if fqdn == 'localhost.localdomain':
                log.error("Error: FQDN resolves to localhost.localdomain")
                return False

        try:
            socket.gethostbyname(hostname)
        except socket.error:
            log.error("Error: Unable to get the ip address for the server name '%s'. "
                      "Please correct the hostname resolution.", hostname)
            return False

        return True

    def _db_accessible(self):
        """Discover whether we have a working connection to the database"""
        from psycopg2 import OperationalError
        from django.db import connection
        try:
            connection.introspection.table_names()
            return True
        except OperationalError:
            connection._rollback()
            return False

    def _db_populated(self):
        """Discover whether the database has this application's tables"""
        from django.db.utils import DatabaseError
        if not self._db_accessible():
            return False
        try:
            from south.models import MigrationHistory
            MigrationHistory.objects.count()
            return True
        except DatabaseError:
            from django.db import connection
            connection._rollback()
            return False

    def _db_current(self):
        """Discover whether there are any outstanding migrations to be
           applied"""
        if not self._db_populated():
            return False

        from south.models import MigrationHistory
        applied_migrations = MigrationHistory.objects.all().values('app_name', 'migration')
        applied_migrations = [(mh['app_name'], mh['migration']) for mh in applied_migrations]

        from south import migration
        for app_migrations in list(migration.all_migrations()):
            for m in app_migrations:
                if (m.app_label(), m.name()) not in applied_migrations:
                    return False
        return True

    def _users_exist(self):
        """Discover whether any users exist in the database"""
        if not self._db_populated():
            return False

        return bool(User.objects.count() > 0)

    def configured(self):
        """Return True if the system has been configured far enough to present
        a user interface"""
        return self._db_current() and self._users_exist()

    def _setup_ntp(self, server = None):
        if not server:
            server = self.get_input(msg = "NTP Server", default = "localhost")
        log.info("Writing ntp configuration")
        ntp = NTPConfig()
        ntp.remove()
        ntp.add(server)
        self._start_ntp()

    def _start_ntp(self):
        log.info("Restarting ntp")
        self.try_shell(["chkconfig", "ntpd", "on"])
        self.try_shell(['service', 'ntpd', 'restart'])

    def _setup_rabbitmq_service(self):
        log.info("Starting RabbitMQ...")
        self.try_shell(["chkconfig", "rabbitmq-server", "on"])
        # FIXME: HYD_640: there's really no sane reason to have to set the stderr and
        #        stdout to None here except that subprocess.PIPE ends up
        #        blocking subprocess.communicate().
        #        we need to figure out why
        self.try_shell(["service", "rabbitmq-server", "restart"],
                       mystderr = None, mystdout = None)

    def _setup_rabbitmq_credentials(self):
        RABBITMQ_USER = "chroma"
        RABBITMQ_PASSWORD = "chroma123"
        RABBITMQ_VHOST = "chromavhost"

        # Enable use from dev_setup as a nonroot user on linux
        sudo = []
        if 'linux' in sys.platform and os.geteuid() != 0:
            sudo = ['sudo']

        rc, out, err = self.try_shell(sudo + ["rabbitmqctl", "-q", "list_users"])
        users = [line.split()[0] for line in out.split("\n") if len(line)]
        if not RABBITMQ_USER in users:
            log.info("Creating RabbitMQ user...")
            self.try_shell(sudo + ["rabbitmqctl", "add_user", RABBITMQ_USER, RABBITMQ_PASSWORD])

        rc, out, err = self.try_shell(sudo + ["rabbitmqctl", "-q", "list_vhosts"])
        vhosts = [line.split()[0] for line in out.split("\n") if len(line)]
        if not RABBITMQ_VHOST in vhosts:
            log.info("Creating RabbitMQ vhost...")
            self.try_shell(sudo + ["rabbitmqctl", "add_vhost", RABBITMQ_VHOST])

        self.try_shell(sudo + ["rabbitmqctl", "set_permissions", "-p", RABBITMQ_VHOST, RABBITMQ_USER, ".*", ".*", ".*"])

        # Enable use of the management plugin if its available, else this tag is just ignored.
        self.try_shell(sudo + ["rabbitmqctl", "set_user_tags", RABBITMQ_USER, "management"])

    def _setup_crypto(self):
        if not os.path.exists(settings.CRYPTO_FOLDER):
            os.makedirs(settings.CRYPTO_FOLDER)
        crypto = Crypto()
        # The server_cert attribute is created on read
        # FIXME: tidy up Crypto, some of its methods are no longer used
        crypto.server_cert

    CONTROLLED_SERVICES = ['chroma-supervisor', 'httpd']

    def _enable_services(self):
        log.info("Enabling Chroma daemons")
        for service in self.CONTROLLED_SERVICES:
            self.try_shell(['chkconfig', '--add', service])

    def _start_services(self):
        log.info("Starting Chroma daemons")
        for service in self.CONTROLLED_SERVICES:
            self.try_shell(['service', service, 'start'])

        SUPERVISOR_START_TIMEOUT = 10
        t = 0
        while True:
            if set([p['statename'] for p in SupervisorStatus().get_all_process_info()]) == set(['RUNNING']):
                break
            else:
                time.sleep(1)
                t += 1
                if t > SUPERVISOR_START_TIMEOUT:
                    msg = "Some services failed to start: %s" % ", ".join(SupervisorStatus().get_non_running_services())
                    log.error(msg)
                    raise RuntimeError(msg)

    def _stop_services(self):
        log.info("Stopping Chroma daemons")
        for service in self.CONTROLLED_SERVICES:
            self.try_shell(['service', service, 'stop'])

        # Wait for supervisord to stop running
        SUPERVISOR_STOP_TIMEOUT = 20
        t = 0
        stopped = False
        while True:
            try:
                SupervisorStatus().get_all_process_info()
            except socket.error:
                # No longer up
                stopped = True
            except xmlrpclib.Fault, e:
                if (e.faultCode, e.faultString) == (6, 'SHUTDOWN_STATE'):
                    # Up but shutting down
                    pass
                else:
                    raise

            if stopped:
                break
            else:
                if t > SUPERVISOR_STOP_TIMEOUT:
                    raise RuntimeError("chroma-supervisor failed to stop after %s seconds" % SUPERVISOR_STOP_TIMEOUT)
                else:
                    t += 1
                    time.sleep(1)

    def _init_pgsql(self, database):
        rc, out, err = self.shell(["service", "postgresql", "initdb"])
        if rc != 0:
            if 'is not empty' not in out:
                log.error("Failed to initialize postgresql service")
                log.error("stdout:\n%s" % out)
                log.error("stderr:\n%s" % err)
                raise CommandError("service postgresql initdb", rc, out, err)
            return
        # Only mess with auth if we've freshly initialized the db
        self._config_pgsql_auth(database)

    def _config_pgsql_auth(self, database):
        auth_cfg_file = "/var/lib/pgsql/data/pg_hba.conf"
        os.rename(auth_cfg_file, "%s.dist" % auth_cfg_file)
        with open(auth_cfg_file, "w") as cfg:
            # Allow our django user to connect with no password
            cfg.write("local\tall\t%s\t\ttrust\n" % database['USER'])
            # Allow the system superuser (postgres) to connect
            cfg.write("local\tall\tall\t\tident\n")

    def _setup_pgsql(self, database):
        log.info("Setting up PostgreSQL service...")
        self._init_pgsql(database)
        self.try_shell(["service", "postgresql", "restart"])
        self.try_shell(["chkconfig", "postgresql", "on"])

        import time
        tries = 0
        while self.shell(["su", "postgres", "-c", "psql -c '\\d'"])[0] != 0:
            if tries >= 4:
                raise RuntimeError("Timed out waiting for PostgreSQL service to start")
            tries += 1
            time.sleep(1)

        if not self._db_accessible():
            log.info("Creating database owner '%s'...\n" % database['USER'])

            # Enumerate existing roles
            _, roles_str, _ = self.try_shell(["su", "postgres", "-c", "psql -t -c 'select rolname from pg_roles;'"])
            roles = [line.strip() for line in roles_str.split("\n") if line.strip()]

            # Create database['USER'] role if not found
            if not database['USER'] in roles:
                self.try_shell(["su", "postgres", "-c", "psql -c 'CREATE ROLE %s NOSUPERUSER CREATEDB NOCREATEROLE INHERIT LOGIN;'" % database['USER']])

            log.info("Creating database '%s'...\n" % database['NAME'])
            self.try_shell(["su", "postgres", "-c", "createdb -O %s %s;" % (database['USER'], database['NAME'])])

    def get_input(self, msg, empty_allowed = True, password = False, default = ""):
        if msg == "":
            raise RuntimeError("Calling get_input, msg must not be empty")

        if default != "":
            msg = "%s [%s]" % (msg, default)

        msg = "%s: " % msg

        answer = ""
        while answer == "":
            if password:
                answer = getpass.getpass(msg)
            else:
                answer = raw_input(msg)

            if answer == "":
                if not empty_allowed:
                    print "A value is required"
                    continue
                if default != "":
                    answer = default
                break

        return answer

    def get_pass(self, msg = "", empty_allowed = True, confirm_msg = ""):
        while True:
            pass1 = self.get_input(msg = msg, empty_allowed = empty_allowed,
                                   password = True)

            pass2 = self.get_input(msg = confirm_msg,
                                   empty_allowed = empty_allowed,
                                   password = True)

            if pass1 != pass2:
                print "Passwords do not match!"
            else:
                return pass1

    def _user_account_prompt(self):
        log.info("Chroma will now create an initial administrative user using the " +
                 "credentials which you provide.")

        valid_username = False
        while not valid_username:
            username = self.get_input(msg = "Chroma superuser", empty_allowed = False)
            if username.find(" ") > -1:
                print "Username cannot contain spaces"
                continue
            valid_username = True
        email = self.get_input(msg = "Email")
        password = self.get_pass(msg = "Password", empty_allowed = False,
                                 confirm_msg = "Confirm password")

        return username, email, password

    def _syncdb(self):
        if not self._db_current():
            log.info("Creating database tables...")
            args = ['', 'syncdb', '--noinput', '--migrate']
            if not self.verbose:
                args = args + ["--verbosity", "0"]
            ManagementUtility(args).execute()
        else:
            log.info("Database tables already OK")

    def _setup_database(self, username = None, password = None):
        if not self._db_accessible():
            # For the moment use the builtin configuration
            # TODO: this is where we would establish DB name and credentials
            databases = settings.DATABASES

            self._setup_pgsql(databases['default'])
        else:
            log.info("DB already accessible")

        self._syncdb()

        if not self._users_exist():
            if not username:
                username, email, password = self._user_account_prompt()
            else:
                email = ""
            user = User.objects.create_superuser(username, email, password)
            user.groups.add(Group.objects.get(name='superusers'))
            log.info("User '%s' successfully created." % username)
        else:
            log.info("User accounts already created")

        # FIXME: we do this here because running management commands requires a working database,
        # but that shouldn't be so (ideally the /static/ dir would be built into the RPM)
        # (Django ticket #17656)
        log.info("Building static directory...")
        args = ['', 'collectstatic', '--noinput']
        if not self.verbose:
            args = args + ["--verbosity", "0"]
        ManagementUtility(args).execute()

    def setup(self, username = None, password = None, ntp_server = None):
        if not self._check_name_resolution():
            return ["Name resolution is not correctly configured"]

        self._setup_database(username, password)
        self._setup_ntp(ntp_server)
        self._setup_rabbitmq_service()
        self._setup_rabbitmq_credentials()
        self._setup_crypto()
        self._enable_services()

        self._start_services()

        return self.validate()

    def start(self):
        if not self._db_current():
            log.error("Cannot start, database not configured")
            return
        self._start_services()

    def stop(self):
        self._stop_services()

    def _service_config(self, interesting_services = None):
        """Interrogate the current status of services"""
        log.info("Checking service configuration...")

        rc, out, err = self.try_shell(['chkconfig', '--list'])
        services = {}
        for line in out.split("\n"):
            if not line:
                continue

            tokens = line.split()
            service_name = tokens[0]
            if interesting_services and service_name not in interesting_services:
                continue

            enabled = (tokens[4][2:] == 'on')

            rc, out, err = self.shell(['service', service_name, 'status'])
            running = (rc == 0)

            services[service_name] = {'enabled': enabled, 'running': running}
        return services

    def validate(self):
        errors = []
        if not self._db_accessible():
            errors.append("Cannot connect to database")
        elif not self._db_current():
            errors.append("Database tables out of date")
        elif not self._users_exist():
            errors.append("No user accounts exist")

        # Check init scripts are up
        interesting_services = self.CONTROLLED_SERVICES + ['postgresql', 'rabbitmq-server']
        service_config = self._service_config(interesting_services)
        for s in interesting_services:
            try:
                service_status = service_config[s]
                if not service_status['enabled']:
                    errors.append("Service %s not set to start at boot" % s)
                if not service_status['running']:
                    errors.append("Service %s is not running" % s)
            except KeyError:
                errors.append("Service %s not found" % s)

        # Check supervisor-controlled services are up
        if service_config['chroma-supervisor']['running']:
            for process in SupervisorStatus().get_all_process_info():
                if process['statename'] != 'RUNNING':
                    errors.append("Service %s is not running (status %s)" % (process['name'], process['statename']))

        return errors

    def _write_local_settings(self, databases):
        # Build a local_settings file
        project_dir = os.path.dirname(os.path.realpath(settings.__file__))
        local_settings = os.path.join(project_dir, settings.LOCAL_SETTINGS_FILE)
        local_settings_str = ""
        local_settings_str += "CELERY_RESULT_BACKEND = \"database\"\n"
        local_settings_str += "CELERY_RESULT_DBURI = \"postgresql://%s:%s@%s%s/%s\"\n" % (
            databases['default']['USER'],
            databases['default']['PASSWORD'],
            databases['default']['HOST'] or "localhost",
            ":%d" % databases['default']['PORT'] if databases['default']['PORT'] else "",
            databases['default']['NAME'])

        # Usefully, a JSON dict looks a lot like python
        local_settings_str += "DATABASES = %s\n" % json.dumps(databases, indent=4).replace("null", "None")

        # Dump local_settings_str to local_settings
        open(local_settings, 'w').write(local_settings_str)

        # TODO: support SERVER_HTTP_URL


def bundle(operation, path=None):
    if operation == "register":
        # Create or update a bundle record
        meta_path = os.path.join(path, "meta")
        try:
            meta = json.load(open(meta_path))
        except (IOError, ValueError):
            raise RuntimeError("Could not read bundle metadata from %s" %
                               meta_path)

        if Bundle.objects.filter(bundle_name=meta['name']).exists():
            Bundle.objects.filter(bundle_name=meta['name']).update(
                location=path, description=meta['description'])
        else:
            Bundle.objects.create(bundle_name=meta['name'],
                                  location=path,
                                  description=meta['description'])
    else:
        # remove bundle record
        try:
            bundle = Bundle.objects.get(location = path)
            bundle.delete()
        except Bundle.DoesNotExist:
            # doesn't exist anyway, so just exit silently
            return


def register_profile(profile_file):
    # create new profile record
    try:
        data = json.load(profile_file)
    except ValueError, e:
        raise RuntimeError("Malformed profile: %s" % e)

    # Validate: check all referenced bundles exist
    validate_bundles = set(data['bundles'] + [bundle for bundle, package in data['packages']])
    missing_bundles = []
    for bundle_name in validate_bundles:
        if not Bundle.objects.filter(bundle_name=bundle_name).exists():
            missing_bundles.append(bundle_name)

    if missing_bundles:
        log.error("Bundles not found for profile '%s': %s" % (data['name'], ", ".join(missing_bundles)))
        sys.exit(-1)

    # Validation OK: create records
    profile = ServerProfile.objects.create(name=data['name'],
                                           ui_name=data['ui_name'],
                                           ui_description=data['ui_description'],
                                           managed=data['managed'])
    for name in data['bundles']:
        profile.bundles.add(Bundle.objects.get(bundle_name=name))

    for bundle_name, package_name in data['packages']:
        ServerProfilePackage.objects.create(
            server_profile=profile,
            bundle=Bundle.objects.get(bundle_name=bundle_name),
            package_name=package_name)


def delete_profile(name):
    # remove profile record
    try:
        profile = ServerProfile.objects.get(name=name)
        profile.delete()
    except ServerProfile.DoesNotExist:
        # doesn't exist anyway, so just exit silently
        return


def default_profile(name):
    """
    Set the default flag on the named profile and clear the default
    flag on all other profiles.

    :param name: A server profile name
    :return: None
    """
    try:
        ServerProfile.objects.get(name=name)
    except ServerProfile.DoesNotExist:
        log.error("Profile '%s' not found" % name)
        sys.exit(-1)

    ServerProfile.objects.update(default=False)
    ServerProfile.objects.filter(name=name).update(default=True)


def chroma_config():
    """Entry point for chroma-config command line tool.

    Distinction between this and ServiceConfig is that CLI-specific stuff lives here:
    ServiceConfig utility methods don't do sys.exit or parse arguments.

    """
    service_config = ServiceConfig()
    try:
        command = sys.argv[1]
    except IndexError:
        log.error("Usage: %s <setup|validate|start|restart|stop>" % sys.argv[0])
        sys.exit(-1)

    def print_errors(errors):
        if errors:
            log.error("Errors found:")
            for error in errors:
                log.error("  * %s" % error)
        else:
            log.info("OK.")

    if command == 'setup':
        def usage():
            log.error("Usage: setup [-v] [username password ntpserver]")
            sys.exit(-1)

        args = []
        if len(sys.argv) > 2:
            if sys.argv[2] == "-v":
                service_config.verbose = True
                if len(sys.argv) == 6:
                    args = sys.argv[3:6]
                elif len(sys.argv) != 3:
                    usage()
            elif len(sys.argv) == 5:
                args = sys.argv[2:5]
            else:
                usage()

        log.info("Starting setup...\n")
        errors = service_config.setup(*args)
        if errors:
            print_errors(errors)
            sys.exit(-1)
        else:
            log.info("\nSetup complete.")
            sys.exit(0)
    elif command == 'validate':
        errors = service_config.validate()
        print_errors(errors)
        if errors:
            sys.exit(1)
        else:
            sys.exit(0)
    elif command == 'stop':
        service_config.stop()
    elif command == 'start':
        service_config.start()
    elif command == 'restart':
        service_config.stop()
        service_config.start()
    elif command == 'bundle':
        operation = sys.argv[2]
        bundle(operation, path = sys.argv[3])
    elif command == 'profile':
        operation = sys.argv[2]
        if operation == 'register':
            try:
                register_profile(open(sys.argv[3]))
            except IOError:
                print "Error opening %s" % sys.argv[3]
        elif operation == 'delete':
            delete_profile(sys.argv[3])
        elif operation == 'default':
            default_profile(sys.argv[3])
        else:
            raise NotImplementedError(operation)
    else:
        log.error("Invalid command '%s'" % command)
        sys.exit(-1)

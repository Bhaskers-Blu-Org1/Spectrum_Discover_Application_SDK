########################################################## {COPYRIGHT-TOP} ###
# Licensed Materials - Property of IBM
# 5737-I32
#
# (C) Copyright IBM Corp. 2019
#
# US Government Users Restricted Rights - Use, duplication, or
# disclosure restricted by GSA ADP Schedule Contract with IBM Corp.
########################################################## {COPYRIGHT-END} ###

import os
import sys
import json
import logging
import subprocess
from subprocess import check_call, CalledProcessError
import tempfile
from io import open
from re import match
from functools import partial
from urllib.parse import urljoin
from confluent_kafka import Consumer, Producer
import requests
import boto3
import paramiko
from .util.aes_cipher import AesCipher

ENCODING = 'utf-8'

class ApplicationBase():
    """Application SDK for registration and communication with Spectrum Discover.

    This script expect configuration parameters to be specified as environment
    variables.

    SPECTRUM_DISCOVER_HOST ..... Spectrum Discover server (domain, IP address)
                                 - default: https://localhost

    APPLICATION_NAME ................. The name of the application to be registered
                                 - default: sd_sample_application

    APPLICATION_USER ................. The user who is used to obtain authentication token
    APPLICATION_USER_PASSWORD

    KAFKA_DIR .................. The directory where TLS certificates will be saved
                                 - absolute or relative path
                                 - default: kafka (relative path to this script)

    LOG_LEVEL .................. Log verbosity level (ERROR, WARNING, INFO, DEBUG)
                                 - default: INFO

    MAX_POLL_INTERVAL .......... kafka config for max.poll.interval.ms
                                 - default: 86400000
    """

    def __init__(self, reg_info):
        """Initialize the ApplicationBase."""
        self.reg_info = reg_info.copy()

        # Instantiate logger
        loglevels = {'INFO': logging.INFO, 'DEBUG': logging.DEBUG,
                     'ERROR': logging.ERROR, 'WARNING': logging.WARNING}
        log_level = os.environ.get('LOG_LEVEL', 'INFO')
        log_format = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        logging.basicConfig(stream=sys.stdout,
                            format=log_format,
                            level=loglevels[log_level])
        self.logger = logging.getLogger(__name__)

        env = lambda envKey, default: os.environ.get(envKey, default)

        # This application name
        self.application_name = env('APPLICATION_NAME', 'sd_sample_application')

        self.is_kube = os.environ.get('KUBERNETES_SERVICE_HOST') is not None

        self.cipher = None

        # The user account assigned to this application
        self.application_token = None

        # Spectrum discover host application talks to
        self.sd_api = env('SPECTRUM_DISCOVER_HOST', 'https://localhost')
        if self.is_kube:
            self.application_user = env('DB2WHREST_USER', '')
            self.application_user_password = env('DB2WHREST_PASSWORD', '')

            self.sd_policy = self._create_host_from_env('POLICY_SERVICE_HOST', 'POLICY_SERVICE_PORT', 'POLICY_PROTOCOL')
            self.sd_connmgr = self._create_host_from_env('CONNMGR_SERVICE_HOST', 'CONNMGR_SERVICE_PORT', 'CONNMGR_PROTOCOL')
            self.sd_auth = env('AUTH_SERVICE_HOST', 'http://auth.spectrum-discover')
        else:
            self.application_user = env('APPLICATION_USER', '')
            self.application_user_password = env('APPLICATION_USER_PASSWORD', '')

            self.sd_policy = env('POLICYENGINE_HOST', self.sd_api)
            self.sd_connmgr = env('CONNMGR_HOST', self.sd_api)
            self.sd_auth = env('AUTH_HOST', self.sd_api)

        # Make sure username and password were set. Everything has a workable default but these two.
        valid_username_password = True
        if not self.application_user:
            self.logger.error("APPLICATION_USER is not set via an environment variable.")
            valid_username_password = False
        if not self.application_user_password:
            self.logger.error("APPLICATION_USER_PASSWORD is not set via an environment variable.")
            valid_username_password = False
        if not valid_username_password:
            raise SystemExit("Missing APPLICATION_USER and or APPLICATION_USER_PASSWORD environment variable.")

        # Endpoints used by application
        policyengine_endpoint = partial(urljoin, self.sd_policy)
        connmgr_endpoint = partial(urljoin, self.sd_connmgr)
        auth_endpoint = partial(urljoin, self.sd_auth)
        cipher_endpoint = partial(urljoin, self.sd_api)
        self.identity_auth_url = auth_endpoint('auth/v1/token')
        self.registration_url = policyengine_endpoint('policyengine/v1/applications')
        self.certificates_url = policyengine_endpoint('policyengine/v1/tlscert')
        self.connmgr_url = connmgr_endpoint('connmgr/v1/internal/connections')
        self.cipher_url = cipher_endpoint('api/application/v1/cipherkey')

        # Certificates directory and file paths
        cert_dir = env('KAFKA_DIR', 'kafka')
        if not os.path.isabs(cert_dir):
            cert_dir = os.path.join(os.getcwd(), cert_dir)

        self.certificates_dir = os.path.normpath(cert_dir)
        cert_path = partial(os.path.join, self.certificates_dir)
        self.kafka_client_cert = cert_path("kafka_client.crt")
        self.kafka_client_key = cert_path("kafka_client.key")
        self.kafka_root_cert = cert_path("kafka-ca.crt")

        # Kafka config - this info comes from registration endpoint
        self.work_q_name = '%s_work' % self.application_name
        self.compl_q_name = '%s_compl' % self.application_name

        # Kafka config - updated as part of registration
        self.kafka_host = None
        self.kafka_ip = None
        self.kafka_port = None
        self.kafka_producer = None
        self.kafka_consumer = None

        # Application running status
        self.application_enabled = False

        # Function that handles messages from Spectrum Discover
        self.message_handler = None

        # a mapping dict of connection to client
        self.connections = {}
        self.conn_details = None

        self.logger.info("Initialize to host: %s", self.sd_api)
        self.logger.info("Application name: %s", self.application_name)
        self.logger.info("Application user: %s", self.application_user)
        self.logger.info("Certificates directory: %s", self.certificates_dir)

        if not self.application_user:
            raise Exception("Authentication requires APPLICATION_USER and APPLICATION_USER_PASSWORD")

        # can be set by apps when starting if they wish to overwrite existing registration info
        self.update_registration = False

    @staticmethod
    def _create_host_from_env(host, port, protocol):

        host = os.environ.get(host, 'localhost')
        protocol = os.environ.get(protocol, 'http')
        port = os.environ.get(port, '80')
        return ('%(protocol)s://%(host)s:%(port)s/' %
                {'protocol': protocol, 'host': host, 'port': port})

    def register_application(self):
        """Attempt to self-register an application and receive an application registration response.

        If the application is already registered a 409 will be returned,
        which means another instance of this application is already registered. In
        that case the application should attempt a GET request to registration endpoint.
        """
        if self.is_kube:
            headers = {
                'Content-Type': 'application/json',
                'X-ALLOW-BASIC-AUTH-SD': 'true'
            }
            auth = requests.auth.HTTPBasicAuth(self.application_user, self.application_user_password)
        else:
            # Get authentication token if not present
            if not self.application_token:
                self.obtain_token()

            headers = {
                'Content-Type': 'application/json',
                'Authorization': 'Bearer %s' % self.application_token
            }
            auth = None

        # Registration request info (insert application name)
        self.reg_info.update({
            "action_agent": self.application_name
        })

        def raise_except_http(valid_codes, http_code):
            if http_code not in valid_codes:
                raise Exception("application:%s, error:%d" % (self.application_name, http_code))

        def post_register():
            response = requests.post(url=self.registration_url, verify=False, json=self.reg_info, headers=headers, auth=auth)

            raise_except_http([200, 201, 409], response.status_code)

            if response.status_code == 409:
                if self.update_registration:
                    self.logger.info('Application already registered, initiating PATCH update request (application:%s)', self.application_name)
                    patch_register()
                    # PATCH does not return any info, need to also GET below
                else:
                    self.logger.warning('Application already registered, initiating GET request (application:%s)', self.application_name)
                return get_register()

            return response.json()

        def patch_register():
            response = requests.patch(url=self.registration_url + '/' + self.application_name,
                                      verify=False, json=self.reg_info, headers=headers, auth=auth)

            raise_except_http([200, 201, 204], response.status_code)

            # A succesful PATCH returns no response
            return

        def get_register():
            response = requests.get(url=self.registration_url, verify=False, headers=headers, auth=auth)

            raise_except_http([200], response.status_code)

            # GET response returns list of registrations
            reg_list = response.json()

            if not reg_list:
                raise Exception('Application GET registration empty - (application:%s)' % self.application_name)

            for reg in reg_list:
                if reg['agent'] == self.application_name:
                    return reg

            return None

        try:
            resp_json = post_register()

            self.update_registration_info(resp_json)
        except Exception as exc:
            self.logger.error(Exception('Application POST registration request FAIL - (%s)' % str(exc)))
            raise

    def update_registration_info(self, reg_response):
        """Record topic names and broker IP/port."""
        self.kafka_ip = reg_response['broker_ip']
        self.kafka_port = reg_response['broker_port']
        self.work_q_name = reg_response['work_q']
        self.compl_q_name = reg_response['completion_q']
        self.kafka_host = "%s:%s" % (self.kafka_ip, self.kafka_port)

        self.logger.info("Application is registered")
        self.logger.info("Kafka host: %s", self.kafka_host)
        self.logger.info("Application attached to work queue: %s", self.work_q_name)
        self.logger.info("Application attached to compl queue: %s", self.compl_q_name)

    def get_kafka_certificates(self):
        """Download the client certificate, client key, and CA root certificate via REST API.

        Parse response and save certificates to files.
        """
        self.logger.info("Download certificates and save to files")

        response = self.download_certificates()

        cert_pattern = "-----BEGIN CERTIFICATE-----[^-]+-----END CERTIFICATE-----"
        key_pattern = "-----BEGIN PRIVATE KEY-----[^-]+-----END PRIVATE KEY-----"
        certs_regex = "(%s)[\n\r]*([^-]+%s)[\n\r]*(%s)" % (cert_pattern, key_pattern, cert_pattern)

        certs = match(certs_regex, response.decode('utf-8'))

        if not certs:
            raise Exception("Cannot parse certificates from response: %s" % response)

        client_cert, client_key, ca_root_cert = certs.groups()

        # Create certificates directory if not exist
        if not os.path.exists(self.certificates_dir):
            os.makedirs(self.certificates_dir)
        elif not os.path.isdir(self.certificates_dir):
            raise Exception("Certificates path is not a directory (%s)" % self.certificates_dir)

        def save_file(file_path, content):
            self.logger.info("Save file: %s", file_path)

            with open(file_path, 'w') as file:
                file.write(content)

        save_file(self.kafka_client_cert, client_cert)
        save_file(self.kafka_client_key, client_key)
        save_file(self.kafka_root_cert, ca_root_cert)

    def download_certificates(self):
        """Download the client certificate, client key, and CA root certificate via REST API.

        Import into the application's trust store.
        """
        self.logger.info("Loading certificates from server: %s", self.certificates_url)

        # Get authentication token if not present
        if self.is_kube:
            headers = {
                'Content-Type': 'application/json',
                'X-ALLOW-BASIC-AUTH-SD': 'true'
            }
            auth = requests.auth.HTTPBasicAuth(self.application_user, self.application_user_password)
        else:
            # Get authentication token if not present
            if not self.application_token:
                self.obtain_token()

            headers = {
                'Content-Type': 'application/json',
                'Authorization': 'Bearer %s' % self.application_token
            }
            auth = None

        try:
            response = requests.get(url=self.certificates_url, verify=False, headers=headers, auth=auth)
            self.logger.debug("CA server response (%s)", response)

            # Return certificates data
            if response.ok:
                return response.content

        except requests.exceptions.HTTPError as exc:
            err = "Http Error :: %s " % exc
        except requests.exceptions.ConnectionError as exc:
            err = "Error Connecting :: %s " % exc
        except requests.exceptions.Timeout as exc:
            err = "Timeout Error :: %s " % exc
        except requests.exceptions.RequestException as exc:
            err = "Request Error :: %s " % exc
        except Exception as exc:
            err = "Request Error :: %s " % str(exc)

        raise Exception(err)

    def configure_kafka(self):
        """Instantiate producer and consumer."""
        # Instantiate producer
        p_conf = {
            'bootstrap.servers': '%s' % self.kafka_host,
            'ssl.certificate.location': self.kafka_client_cert,
            'ssl.key.location': self.kafka_client_key,
            'security.protocol': 'ssl', 'ssl.ca.location': self.kafka_root_cert}

        self.kafka_producer = Producer(p_conf)

        # Instantiate consumer
        max_poll_interval = int(os.environ.get('MAX_POLL_INTERVAL', 86400000))
        c_conf = {
            'bootstrap.servers': '%s' % self.kafka_host,
            'group.id': 'myagent_grp',
            'session.timeout.ms': 6000,
            'max.poll.interval.ms': max_poll_interval,
            'default.topic.config': {'auto.offset.reset': 'smallest'},
            'ssl.certificate.location': self.kafka_client_cert,
            'ssl.key.location': self.kafka_client_key,
            'enable.auto.commit': 'false',
            'security.protocol': 'ssl', 'ssl.ca.location': self.kafka_root_cert}

        self.kafka_consumer = Consumer(c_conf)

    def obtain_token(self):
        """Retrieve role based token for authentication.

        Any application SDK requests to policy engine require role based
        token authentication. The user role assigned to this application must have an
        authenticated account on the server created externally by an
        admin.
        """
        self.logger.info('Application obtaining token from URL: %s', self.identity_auth_url)

        try:
            headers = {}
            if self.is_kube:
                headers['X-ALLOW-BASIC-AUTH-SD'] = 'true'
            basic_auth = requests.auth.HTTPBasicAuth(self.application_user, self.application_user_password)
            response = requests.get(url=self.identity_auth_url, verify=False, headers=headers, auth=basic_auth)
            # check response from identity auth server
            if response.status_code == 200:
                self.application_token = response.headers['X-Auth-Token']
                self.logger.info('Application token retrieved: %s...', self.application_token[:10])
                return self.application_token

            raise Exception("Attempt to obtain token returned (%d)" % response.status_code)
        except Exception as exc:
            self.logger.error('Application failed to obtain token (%s)', str(exc))
            raise
        return

    def get_connection_details(self):
        """Read the connection details from Spectrum Discover.

        Store them for future file retrieval. May require setup - sftp connections or
        nfs mounts.
        """
        self.logger.debug("Querying information for connections")
        try:

            headers = {}
            self.logger.info("Invoking conn manager at %s", self.connmgr_url)

            if self.is_kube:
                headers['X-ALLOW-BASIC-AUTH-SD'] = 'true'
                auth = requests.auth.HTTPBasicAuth(self.application_user, self.application_user_password)
            else:
                if not self.application_token:
                    self.obtain_token()
                headers['Authorization'] = 'Bearer ' + self.application_token
                auth = None

            response = requests.get(url=self.connmgr_url, verify=False, headers=headers, auth=auth)

            cipherkey = os.environ.get('CIPHER_KEY', None)
            if cipherkey:
                self.cipher = AesCipher(cipherkey)
            else:
                cipherkey_response = requests.get(url=self.cipher_url, verify=False, headers=headers, auth=auth)
                if cipherkey_response.ok:
                    self.cipher = AesCipher(cipherkey_response.json()['cipher_key'])
                else:
                    self.logger.warning("Cipher key was not available. This may affect cos and scale connections")

            self.logger.debug("Connection Manager response (%s)", response)

            # return certificate data
            if response.ok:
                return json.loads(response.content)

        except requests.exceptions.HTTPError as exc:
            err = "Http Error :: %s " % exc
        except requests.exceptions.ConnectionError as exc:
            err = "Error Connecting :: %s " % exc
        except requests.exceptions.Timeout as exc:
            err = "Timeout Error :: %s " % exc
        except requests.exceptions.RequestException as exc:
            err = "Request Error :: %s " % exc
        except Exception as exc:
            err = "Request Error :: %s " % str(exc)

        raise Exception(err)

    def call_manager_api(self, url, manager_username, manager_password):
        """Execute a GET on the Manager API and handle the response."""
        try:
            response = requests.get(url, auth=(manager_username, manager_password))

            if response is None:
                self.logger.error("This manager site cannot be reached: %s. ", url)

            if not response.ok:
                self.logger.error("Failed to connect to %s. Response status: %s %s. ",
                                  url, response.status_code, response.reason)
            return response
        except Exception as err:
            self.logger.error("Error type %s when getting COS credentials", type(err))

    def manager_api_get_aws_keys(self, manager_ip, manager_username, manager_password):
        """Get AWS keys from the manager API.

        Calls the manager api listMyAccessKeys.adm.
        """
        url = "https://{0}/manager/api/json/1.0/listMyAccessKeys.adm".format(manager_ip)
        response = self.call_manager_api(url, manager_username, manager_password)

        try:
            # Get the first access/secret key.
            keys = response.json()['responseData']['accessKeys']
            if keys:
                self.logger.info("Accesser credentials successfully retrieved from Manager API")
                accesser_access_key = keys[0]['accessKeyId']
                accesser_secret_key = keys[0]['secretAccessKey']
            else:
                accesser_access_key = None
                accesser_secret_key = None

            return (accesser_access_key, accesser_secret_key)
        except Exception as err:
            self.logger.error("Error type %s when parsing COS credentials", type(err))
            return (None, None)

    def create_cos_connection(self, conn):
        """Create a COS connection for retrieving docs."""
        additional_info = json.loads(conn['additional_info'])
        aws_access_key_id = additional_info.get('accesser_access_key', None)
        aws_secret_access_key = additional_info.get('accesser_secret_key', None)

        try:
            if not aws_access_key_id or not aws_secret_access_key:
                # If access keys are not supplied and manager credentials are
                # then retrieve the keys via the management interface
                manager_username = additional_info.get('manager_username', None)
                manager_password = additional_info.get('manager_password', None)
                if conn['host'] and manager_username and manager_password:
                    manager_password = self.cipher.decrypt(manager_password)
                    (aws_access_key_id, aws_secret_access_key) = self.manager_api_get_aws_keys(conn['host'], manager_username,
                                                                                               manager_password)
            else:
                aws_secret_access_key = self.cipher.decrypt(aws_secret_access_key)
        except Exception as err:
            self.logger.error("Credentials problem '%s' with COS connection %s", str(err), conn['name'])

        client = boto3.client(
            's3',
            endpoint_url='http://' + additional_info['accesser_address'],
            aws_access_key_id=aws_access_key_id,
            aws_secret_access_key=aws_secret_access_key
        )

        self.connections[(conn['datasource']), conn['cluster']] = ('COS', client)
        self.logger.info('Successfully created cos connection for: %s', conn['name'])

    def mount_nfs(self, local_mount, host):
        """Mount the NFS file system."""
        if not host:
            raise IOError('Host not defined so cannot create NFS mount.')

        if not os.path.ismount(local_mount):
            try:
                check_call('mkdir -p {local_mount}'.format(local_mount=local_mount), shell=True)
                check_call('mount -t nfs -o nolock -o ro {host} {local_mount}'
                           .format(host=host, local_mount=local_mount), shell=True)
                self.logger.info('Mounted remote NFS folder %s', host)
            except CalledProcessError:
                # Not fatal, this might not be an active connection
                self.logger.warning('Failed to mount remote NFS folder %s', host)

    def create_nfs_connection(self, conn):
        """Create a NFS connection for retrieving docs using mount point."""
        additional_info = json.loads(conn['additional_info'])

        remote_nfs_mount = conn['host'] + ':' + conn['mount_point']
        mount_path_prefix = additional_info['local_mount']

        self.mount_nfs(mount_path_prefix, remote_nfs_mount)
        # need to store this to correlate connections in work messages
        conn['additional_info'] = additional_info

        self.connections[(conn['datasource']), conn['cluster']] = ('NFS', conn)
        self.logger.info('Successfully created nfs connection for: %s', conn['name'])

    def create_scale_connection(self, conn):
        """Create a Scale connection for retrieving docs using sftp and RSA key."""
        if not conn['online']:
            self.logger.info('Skipping creation of offline scale connection: %s', conn['host'])
            return
        if not conn['password']:
            self.logger.warning('Skipping creation of scale connection: %s due to missing password', conn['host'])
            return

        try:
            xport = paramiko.Transport(conn['host'])

            local_conn = False
            try:
                proc = subprocess.Popen(['/usr/lpp/mmfs/bin/mmlscluster'], stdout=subprocess.PIPE)
                stdout, _ = proc.communicate()
                if conn['cluster'] in stdout.decode(ENCODING):
                    local_conn = True
            except Exception:
                pass

            if local_conn:
                self.connections[(conn['datasource']), conn['cluster']] = ('Spectrum Scale Local', conn)
                self.logger.info('Successfully created local scale connection for: %s', conn['name'])
                return

            xport.connect(username=conn['user'], password=self.cipher.decrypt(conn['password']))
            sftp = paramiko.SFTPClient.from_transport(xport)
            if sftp:
                self.connections[(conn['datasource']), conn['cluster']] = ('Spectrum Scale', sftp)
                self.logger.info('Successfully created scale connection for: %s', conn['name'])

        except (paramiko.ssh_exception.BadHostKeyException, paramiko.ssh_exception.AuthenticationException,
                paramiko.ssh_exception.SSHException, paramiko.ssh_exception.NoValidConnectionsError) as ex:
            self.logger.warning('Error when attempting Scale connection: %s', str(ex))

    def mount_smb(self, conn, local_mount):
        """Mount the SMB file system."""
        if not conn['host']:
            self.logger.warning('Host not defined so cannot create SMB mount.')
            return
        if not conn['user']:
            self.logger.warning('Could not retrieve SMB user.')
            return
        if not conn['password']:
            self.logger.warning('Could not retrieve password for SMB mount.')
            return
        if not conn['mount_point']:
            self.logger.warning('Could not retrieve smb export path.')
            return
        if not local_mount:
            self.logger.warning('Host mount path could not be created.')
            return

        host = conn['host']
        password = self.cipher.decrypt(conn['password'])
        export_path = conn['mount_point']

        if '\\' in conn['user']:
            (domain, user) = conn['user'].split('\\')
        elif '/' in conn['user']:
            (domain, user) = conn['user'].split('/')
        elif '@' in conn['user']:
            (user, domain) = conn['user'].split('@')
        else:
            (domain, user) = ('', conn['user'])

        cmd = f'mount -t cifs {export_path} {local_mount} -o user={user} -o password={password} -o ro'
        if domain:
            cmd += f' -o domain={domain}'

        try:
            check_call(cmd, shell=True)
        except CalledProcessError:
            # Not fatal, this might not be an active connection
            self.logger.warning('Failed to mount SMB export %s', host)

    def create_smb_connection(self, conn):
        """Create a SMB connection for retrieving docs using a cifs mount."""
        prefix = 'smb_'
        suffix = '_' + conn['name'] + '_' + conn['datasource']
        local_mount = tempfile.mkdtemp(suffix=suffix, prefix=prefix)

        conn['additional_info'] = {}
        conn['additional_info']['local_mount'] = local_mount

        self.mount_smb(conn, local_mount)
        self.connections[(conn['datasource']), conn['cluster']] = ('SMB/CIFS', conn)
        self.logger.info('Successfully created smb connection for: %s', conn['name'])

    def connect_to_datasources(self):
        """Loop through datasources and create connections."""
        self.conn_details = self.get_connection_details()
        for conn in self.conn_details:
            if conn['platform'] == "IBM COS":
                self.create_cos_connection(conn)
            elif conn['platform'] == "NFS":
                self.create_nfs_connection(conn)
            elif conn['platform'] == "Spectrum Scale":
                self.create_scale_connection(conn)
            elif conn['platform'] == "SMB/CIFS":
                self.create_smb_connection(conn)
            else:
                self.logger.warning("Unsupported connection platform %s", conn['platform'])

    def start(self, update_registration=False):
        """Start Application."""
        self.logger.info("Starting Spectrum Discover application...")

        # Set application running status
        self.application_enabled = True

        # If app is already registered, replace registration info with the new one when this flag is set
        self.update_registration = update_registration

        # Register this application to Spectrum Discover
        self.register_application()

        # Get Kafka certificates from Spectrum Discover
        self.get_kafka_certificates()

        # Get connections for data retrieval
        self.connect_to_datasources()

        # Instantiate Kafka producer and consumer
        self.configure_kafka()

        # Auth token will expire, remove existing so that later requests will get the new one
        self.application_token = None

    def stop(self):
        """Stop Application."""
        self.logger.info("Stopping Spectrum Discover application...")

        # Disable application
        self.application_enabled = False

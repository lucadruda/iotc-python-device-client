__path__ = __import__("pkgutil").extend_path(__path__, __name__)


import sys
import threading
import time
from azure.iot.device import X509
from azure.iot.device import IoTHubDeviceClient
from azure.iot.device import ProvisioningDeviceClient
from azure.iot.device import Message, MethodResponse
from datetime import datetime

__version__ = "0.2.0-beta.4"
__name__ = "azure-iotcentral-device-client"


def version():
    print(__version__)


try:
    import hmac
except ImportError:
    print("ERROR: missing dependency `micropython-hmac`")
    sys.exit()

try:
    import hashlib
except ImportError:
    print("ERROR: missing dependency `micropython-hashlib`")
    sys.exit()

try:
    import base64
except ImportError:
    print("ERROR: missing dependency `micropython-base64`")
    sys.exit()

try:
    import json
except ImportError:
    print("ERROR: missing dependency `micropython-json`")
    sys.exit()

try:
    import uuid
except ImportError:
    print("ERROR: missing dependency `micropython-uuid`")
    sys.exit()

gIsMicroPython = ('implementation' in dir(sys)) and ('name' in dir(
    sys.implementation)) and (sys.implementation.name == 'micropython')


class IOTCConnectType:
    IOTC_CONNECT_SYMM_KEY = 1
    IOTC_CONNECT_X509_CERT = 2
    IOTC_CONNECT_DEVICE_KEY = 3



class IOTCLogLevel:
    IOTC_LOGGING_DISABLED = 1
    IOTC_LOGGING_API_ONLY = 2
    IOTC_LOGGING_ALL = 16


class IOTCConnectionState:
    IOTC_CONNECTION_EXPIRED_SAS_TOKEN = 1
    IOTC_CONNECTION_DEVICE_DISABLED = 2
    IOTC_CONNECTION_BAD_CREDENTIAL = 4
    IOTC_CONNECTION_RETRY_EXPIRED = 8
    IOTC_CONNECTION_NO_NETWORK = 16
    IOTC_CONNECTION_COMMUNICATION_ERROR = 32
    IOTC_CONNECTION_OK = 64


class IOTCMessageStatus:
    IOTC_MESSAGE_ACCEPTED = 1
    IOTC_MESSAGE_REJECTED = 2
    IOTC_MESSAGE_ABANDONED = 4


class IOTCEvents:
    IOTC_COMMAND = 2,
    IOTC_PROPERTIES = 4,


class ConsoleLogger:
    def __init__(self, logLevel):
        self._logLevel = logLevel

    def _log(self, message):
        print(message)

    def info(self, message):
        if self._logLevel != IOTCLogLevel.IOTC_LOGGING_DISABLED:
            self._log(message)

    def debug(self, message):
        if self._logLevel == IOTCLogLevel.IOTC_LOGGING_ALL:
            self._log(message)

    def setLogLevel(self, logLevel):
        self._logLevel = logLevel


class IoTCClient:
    def __init__(self, deviceId, scopeId, credType, keyOrCert, logger=None):
        self._deviceId = deviceId
        self._scopeId = scopeId
        self._credType = credType
        self._keyORCert = keyOrCert
        self._modelId = None
        self._connected = False
        self._events = {}
        self._propThread = None
        self._cmdThread = None
        self._globalEndpoint = "global.azure-devices-provisioning.net"
        if logger is None:
            self._logger = ConsoleLogger(IOTCLogLevel.IOTC_LOGGING_API_ONLY)
        else:
            self._logger = logger

    def isConnected(self):
        """
        Check if device is connected to IoTCentral
        :returns: Connection state
        :rtype: bool
        """
        if self._connected:
            return True
        else:
            return False


    def setGlobalEndpoint(self, endpoint):
        """
        Set the device provisioning endpoint.
        :param str endpoint: Custom device provisioning endpoint. Default ('global.azure-devices-provisioning.net')
        """
        self._globalEndpoint = endpoint

    def setModelId(self, modelId):
        """
        Set the model Id for the device to be associated
        :param str modelId: Id for an existing model in the IoTCentral app
        """
        self._modelId = modelId

    def setLogLevel(self, logLevel):
        """
        Set the logging level
        :param IOTCLogLevel: Logging level. Available options are: ALL, API_ONLY, DISABLE
        """
        self._logger.setLogLevel(logLevel)

    def on(self, eventname, callback):
        """
        Set a listener for a specific event
        :param IOTCEvents eventname: Supported events: IOTC_PROPERTIES, IOTC_COMMANDS
        :param function callback: Function executed when the specified event occurs
        """
        self._events[eventname] = callback
        return 0

    def _onProperties(self):
        self._logger.debug('Setup properties listener')
        while True:
            try:
                propCb = self._events[IOTCEvents.IOTC_PROPERTIES]
            except KeyError:
                self._logger.debug('Properties callback not found')
                time.sleep(10)
                continue
            
            patch = self._deviceClient.receive_twin_desired_properties_patch()
            self._logger.debug('\nReceived desired properties. {}\n'.format(patch))
            
            for prop in patch:
                if prop == '$version':
                    continue

                ret = propCb(prop, patch[prop]['value'])
                if ret:
                    self._logger.debug('Acknowledging {}'.format(prop))
                    self.sendProperty({
                        '{}'.format(prop): {
                            "value": patch[prop]["value"],
                            'status': 'completed',
                            'desiredVersion': patch['$version'],
                            'message': 'Property received'}
                    })
                else:
                    self._logger.debug(
                        'Property "{}" unsuccessfully processed'.format(prop))

    def _cmdAck(self,name, value, requestId):
        self.sendProperty({
            '{}'.format(name): {
                'value': value,
                'requestId': requestId
            }
        })
    
    def _onCommands(self):
        self._logger.debug('Setup commands listener')
        while True:
            try:
                cmdCb = self._events[IOTCEvents.IOTC_COMMAND]
            except KeyError:
                self._logger.debug('Commands callback not found')
                time.sleep(10)
                continue
            # Wait for unknown method calls
            method_request = self._deviceClient.receive_method_request()
            self._logger.debug(
                'Received command {}'.format(method_request.name))
            self._deviceClient.send_method_response(MethodResponse.create_from_method_request(
                method_request, 200, {
                    'result': True, 'data': 'Command received'}
            ))
            cmdCb(method_request,self._cmdAck)
            

    def _sendMessage(self, payload, properties, callback=None):
        msg = Message(payload)
        msg.message_id = uuid.uuid4()
        if bool(properties):
            for prop in properties:
                msg.custom_properties[prop] = properties[prop]
        self._deviceClient.send_message(msg)
        if callback is not None:
            callback()

    def sendProperty(self, payload, callback=None):
        """
        Send a property message
        :param dict payload: The properties payload. Can contain multiple properties in the form {'<propName>':{'value':'<propValue>'}}
        :param function callback: Function executed after successfull dispatch
        """
        self._logger.debug('Sending property {}'.format(json.dumps(payload)))
        self._deviceClient.patch_twin_reported_properties(payload)
        if callback is not None:
            callback()

    def sendTelemetry(self, payload, properties=None, callback=None):
        """
        Send a telemetry message
        :param dict payload: The telemetry payload. Can contain multiple telemetry fields in the form {'<fieldName1>':<fieldValue1>,...,'<fieldNameN>':<fieldValueN>}
        :param dict optional properties: An object with custom properties to add to the message.
        :param function callback: Function executed after successfull dispatch
        """
        self._logger.info('Sending telemetry message: {}'.format(payload))
        self._sendMessage(json.dumps(payload), properties, callback)

    def connect(self):
        """
        Connects the device.
        :raises exception: If connection fails
        """
        if self._credType in (IOTCConnectType.IOTC_CONNECT_DEVICE_KEY, IOTCConnectType.IOTC_CONNECT_SYMM_KEY):
            if self._credType == IOTCConnectType.IOTC_CONNECT_SYMM_KEY:
                self._keyORCert = self._computeDerivedSymmetricKey(
                    self._keyORCert, self._deviceId)
                self._logger.debug('Device key: {}'.format(self._keyORCert))

            self._provisioningClient = ProvisioningDeviceClient.create_from_symmetric_key(
                    self._globalEndpoint, self._deviceId, self._scopeId, self._keyORCert)
        else:
            self._keyfile = self._keyORCert['keyFile']
            self._certfile = self._keyORCert['certFile']
            try:
                self._certPhrase=self._keyORCert['certPhrase']
                x509=X509(self._certfile,self._keyfile,self._certPhrase)
            except:
                self._logger.debug('No passphrase available for certificate. Trying without it')
                x509=X509(self._certfile,self._keyfile)
            # Certificate provisioning
            self._provisioningClient=ProvisioningDeviceClient.create_from_x509_certificate(provisioning_host=self._globalEndpoint,registration_id=self._deviceId,id_scope=self._scopeId,x509=x509)

        if self._modelId:
            self._provisioningClient.provisioning_payload = {
                'iotcModelId': self._modelId}
        try:
            registration_result = self._provisioningClient.register()
            assigned_hub = registration_result.registration_state.assigned_hub
            self._logger.debug(assigned_hub)
            self._hubCString = 'HostName={};DeviceId={};SharedAccessKey={}'.format(
                assigned_hub, self._deviceId, self._keyORCert)
            self._logger.debug(
                'IoTHub Connection string: {}'.format(self._hubCString))

            if self._credType in (IOTCConnectType.IOTC_CONNECT_DEVICE_KEY, IOTCConnectType.IOTC_CONNECT_SYMM_KEY):
                self._deviceClient = IoTHubDeviceClient.create_from_connection_string(self._hubCString)
            else:
                self._deviceClient = IoTHubDeviceClient.create_from_x509_certificate(x509=x509,hostname=assigned_hub,device_id=registration_result.registration_state.device_id)
        except:
            t, v, tb = sys.exc_info()
            self._logger.info(
                'ERROR: Failed to get device provisioning information')
            raise t(v)
        # Connect to iothub
        try:
            self._deviceClient.connect()
            self._connected = True
            self._logger.debug('Device connected')
        except:
            t, v, tb = sys.exc_info()
            self._logger.info('ERROR: Failed to connect to Hub')
            raise t(v)

        # setup listeners

        self._propThread = threading.Thread(target=self._onProperties)
        self._propThread.daemon = True
        self._propThread.start()

        self._cmdThread = threading.Thread(target=self._onCommands)
        self._cmdThread.daemon = True
        self._cmdThread.start()

    def _computeDerivedSymmetricKey(self, secret, regId):
        # pylint: disable=no-member
        global gIsMicroPython
        try:
            secret = base64.b64decode(secret)
        except:
            self._logger.debug(
                "ERROR: broken base64 secret => `" + secret + "`")
            sys.exit()

        if gIsMicroPython == False:
            return base64.b64encode(hmac.new(secret, msg=regId.encode('utf8'), digestmod=hashlib.sha256).digest()).decode('utf-8')
        else:
            return base64.b64encode(hmac.new(secret, msg=regId.encode('utf8'), digestmod=hashlib._sha256.sha256).digest())

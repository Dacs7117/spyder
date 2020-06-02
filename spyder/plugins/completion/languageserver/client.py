# -*- coding: utf-8 -*-

# Copyright © Spyder Project Contributors
# Licensed under the terms of the MIT License
# (see spyder/__init__.py for details)

"""
Spyder Language Server Protocol Client implementation.

This client implements the calls and procedures required to
communicate with a v3.0 Language Server Protocol server.
"""

# Standard library imports
import logging
import os
import os.path as osp
import signal
import sys
import time

# Third-party imports
from qtpy.QtCore import (QObject, QProcess, QProcessEnvironment,
                         QSocketNotifier, Signal, Slot)
import zmq
import psutil

# Local imports
from spyder.config.base import (DEV, get_conf_path, get_debug_level,
                                running_under_pytest)
from spyder.plugins.completion.languageserver import (
    CLIENT_CAPABILITES, SERVER_CAPABILITES,
    TEXT_DOCUMENT_SYNC_OPTIONS, LSPRequestTypes,
    ClientConstants)
from spyder.plugins.completion.languageserver.decorators import (
    send_request, send_notification, class_register, handles)
from spyder.plugins.completion.languageserver.transport import MessageKind
from spyder.plugins.completion.languageserver.providers import (
    LSPMethodProviderMixIn)
from spyder.py3compat import PY2
from spyder.utils.misc import getcwd_or_home, select_port

# Conditional imports
if PY2:
    import pathlib2 as pathlib
else:
    import pathlib

# Main constants
LOCATION = osp.realpath(osp.join(os.getcwd(),
                                 osp.dirname(__file__)))
PENDING = 'pending'
SERVER_READY = 'server_ready'
LOCALHOST = '127.0.0.1'

# Language server communication verbosity at server logs.
TRACE = 'messages'
if DEV:
    TRACE = 'verbose'

logger = logging.getLogger(__name__)


@class_register
class LSPClient(QObject, LSPMethodProviderMixIn):
    """Language Server Protocol v3.0 client implementation."""
    #: Signal to inform the editor plugin that the client has
    #  started properly and it's ready to be used.
    sig_initialize = Signal(dict, str)

    #: Signal to report internal server errors through Spyder's
    #  facilities.
    sig_server_error = Signal(str)

    #: Signal to warn the user when either the transport layer or the
    #  server went down
    sig_went_down = Signal(str)

    def __init__(self, parent,
                 server_settings={},
                 folder=getcwd_or_home(),
                 language='python'):
        QObject.__init__(self)
        self.manager = parent
        self.zmq_in_socket = None
        self.zmq_out_socket = None
        self.zmq_in_port = None
        self.zmq_out_port = None
        self.transport = None
        self.server = None
        self.stdio_pid = None
        self.notifier = None
        self.language = language

        self.initialized = False
        self.ready_to_close = False
        self.request_seq = 1
        self.req_status = {}
        self.watched_files = {}
        self.watched_folders = {}
        self.req_reply = {}

        # Select a free port to start the server.
        # NOTE: Don't use the new value to set server_setttings['port']!!
        # That's not required because this doesn't really correspond to a
        # change in the config settings of the server. Else a server
        # restart would be generated when doing a
        # workspace/didChangeConfiguration request.
        if not server_settings['external']:
            self.server_port = select_port(
                default_port=server_settings['port'])
        else:
            self.server_port = server_settings['port']
        self.server_host = server_settings['host']

        self.transport_args = [sys.executable, '-u',
                               osp.join(LOCATION, 'transport', 'main.py')]
        self.external_server = server_settings.get('external', False)
        self.stdio = server_settings.get('stdio', False)

        # Setting stdio on implies that external_server is off
        if self.stdio and self.external_server:
            error = ('If server is set to use stdio communication, '
                     'then it cannot be an external server')
            logger.error(error)
            raise AssertionError(error)

        self.folder = folder
        self.plugin_configurations = server_settings.get('configurations', {})
        self.client_capabilites = CLIENT_CAPABILITES
        self.server_capabilites = SERVER_CAPABILITES
        self.context = zmq.Context()

        server_args_fmt = server_settings.get('args', '')
        server_args = server_args_fmt.format(
            host=self.server_host,
            port=self.server_port)
        transport_args_fmt = '--server-host {host} --server-port {port} '
        transport_args = transport_args_fmt.format(
            host=self.server_host,
            port=self.server_port)

        self.server_args = []
        if self.language == 'python':
            self.server_args += [sys.executable, '-m']
        self.server_args += [server_settings['cmd']]
        if len(server_args) > 0:
            self.server_args += server_args.split(' ')
        self.server_unresponsive = False

        self.transport_args += transport_args.split(' ')
        self.transport_args += ['--folder', folder]
        self.transport_args += ['--transport-debug', str(get_debug_level())]
        if not self.stdio:
            self.transport_args += ['--external-server']
        else:
            self.transport_args += ['--stdio-server']
            self.external_server = True
        self.transport_unresponsive = False

    def create_transport_sockets(self):
        """Create PyZMQ sockets for transport."""
        self.zmq_out_socket = self.context.socket(zmq.PAIR)
        self.zmq_out_port = self.zmq_out_socket.bind_to_random_port(
            'tcp://{}'.format(LOCALHOST))
        self.zmq_in_socket = self.context.socket(zmq.PAIR)
        self.zmq_in_socket.set_hwm(0)
        self.zmq_in_port = self.zmq_in_socket.bind_to_random_port(
            'tcp://{}'.format(LOCALHOST))
        self.transport_args += ['--zmq-in-port', self.zmq_out_port,
                                '--zmq-out-port', self.zmq_in_port]

    @Slot(QProcess.ProcessError)
    def handle_process_errors(self, error):
        """Handle errors with the transport layer or server processes."""
        self.sig_went_down.emit(self.language)

    def start_server(self):
        """Start server."""
        # This is not necessary if we're trying to connect to an
        # external server
        if self.external_server:
            return

        # Set server log file
        server_log_file = None
        if get_debug_level() > 0:
            # Create server log file
            server_log_fname = 'server_{0}_{1}.log'.format(self.language,
                                                           os.getpid())
            server_log_file = get_conf_path(osp.join('lsp_logs',
                                                     server_log_fname))

            if not osp.exists(osp.dirname(server_log_file)):
                os.makedirs(osp.dirname(server_log_file))

            if self.stdio:
                if self.language == 'python':
                    self.server_args += ['--log-file', server_log_file]
                self.transport_args += ['--server-log-file', server_log_file]

            # Start server with logging options
            if self.language == 'python':
                if get_debug_level() == 2:
                    self.server_args.append('-v')
                elif get_debug_level() == 3:
                    self.server_args.append('-vv')

        logger.info('Starting server: {0}'.format(' '.join(self.server_args)))

        # Set the PyLS current working to an empty dir inside
        # our config one. This avoids the server to pick up user
        # files such as random.py or string.py instead of the
        # standard library modules named the same.
        if self.language == 'python':
            cwd = get_conf_path('empty_cwd')
            if not osp.exists(cwd):
                os.mkdir(cwd)
        else:
            cwd = None

        # Setup server
        self.server = QProcess(self)
        self.server.errorOccurred.connect(self.handle_process_errors)
        self.server.setWorkingDirectory(cwd)
        self.server.setProcessChannelMode(QProcess.MergedChannels)
        if server_log_file is not None:
            self.server.setStandardOutputFile(server_log_file)

        # Start server
        self.server.start(self.server_args[0], self.server_args[1:])

    def start_transport(self):
        """Start transport layer."""
        self.transport_args = list(map(str, self.transport_args))
        logger.info('Starting transport: {0}'
                    .format(' '.join(self.transport_args)))

        self.transport = QProcess(self)
        self.transport.errorOccurred.connect(self.handle_process_errors)

        # Modifying PYTHONPATH to run transport in development mode or
        # tests
        if DEV or running_under_pytest():
            env = QProcessEnvironment()
            if running_under_pytest():
                env.insert('PYTHONPATH', os.pathsep.join(sys.path)[:])
            else:
                env.insert('PYTHONPATH', os.pathsep.join(sys.path)[1:])
            self.transport.setProcessEnvironment(env)

        # Set transport log file
        transport_log_file = None
        if get_debug_level() > 0:
            transport_log_fname = 'transport_{0}_{1}.log'.format(
                self.language, os.getpid())
            transport_log_file = get_conf_path(
                osp.join('lsp_logs', transport_log_fname))
            if not osp.exists(osp.dirname(transport_log_file)):
                os.makedirs(osp.dirname(transport_log_file))

        # Set channel properties
        if self.stdio:
            self.transport_args += self.server_args
            self.transport.setProcessChannelMode(QProcess.SeparateChannels)
            if transport_log_file is not None:
                self.transport.setStandardErrorFile(transport_log_file)
        else:
            self.transport.setProcessChannelMode(QProcess.MergedChannels)
            if transport_log_file is not None:
                self.transport.setStandardOutputFile(transport_log_file)

        # Start transport
        self.transport.start(self.transport_args[0], self.transport_args[1:])

    def start(self):
        """Start client."""
        # NOTE: DO NOT change the order in which these methods are called.
        self.create_transport_sockets()
        self.start_server()
        self.start_transport()

        # Create notifier
        fid = self.zmq_in_socket.getsockopt(zmq.FD)
        self.notifier = QSocketNotifier(fid, QSocketNotifier.Read, self)
        self.notifier.activated.connect(self.on_msg_received)

        # This is necessary for tests to pass locally!
        logger.debug('LSP {} client started!'.format(self.language))

    def stop(self):
        """Stop transport and server."""
        logger.info('Stopping {} client...'.format(self.language))
        if self.notifier is not None:
            self.notifier.activated.disconnect(self.on_msg_received)
            self.notifier.setEnabled(False)
            self.notifier = None
        if self.transport is not None:
            self.transport.kill()
        self.context.destroy()
        if self.server is not None:
            self.server.kill()

    def is_transport_alive(self):
        """Detect if transport layer is alive."""
        state = self.transport.state()
        if state == QProcess.Starting:
            return True
        else:
            return state != QProcess.NotRunning

    def is_stdio_alive(self):
        """Check if an stdio server is alive."""
        alive = True
        if not psutil.pid_exists(self.stdio_pid):
            alive = False
        else:
            try:
                pid_status = psutil.Process(self.stdio_pid).status()
            except psutil.NoSuchProcess:
                pid_status = ''
            if pid_status == psutil.STATUS_ZOMBIE:
                alive = False
        return alive

    def is_server_alive(self):
        """Detect if a tcp server is alive."""
        state = self.server.state()
        if state == QProcess.Starting:
            return True
        else:
            return state != QProcess.NotRunning

    def is_down(self):
        """
        Detect if the transport layer or server are down to inform our
        users about it.
        """
        is_down = False
        if self.transport and not self.is_transport_alive():
            logger.debug(
                "Transport layer for {} is down!!".format(self.language))
            if not self.transport_unresponsive:
                self.transport_unresponsive = True
                self.sig_went_down.emit(self.language)
            is_down = True

        if self.server and not self.is_server_alive():
            logger.debug("LSP server for {} is down!!".format(self.language))
            if not self.server_unresponsive:
                self.server_unresponsive = True
                self.sig_went_down.emit(self.language)
            is_down = True

        if self.stdio_pid and not self.is_stdio_alive():
            logger.debug("LSP server for {} is down!!".format(self.language))
            if not self.server_unresponsive:
                self.server_unresponsive = True
                self.sig_went_down.emit(self.language)
            is_down = True

        return is_down

    def send(self, method, params, kind):
        """Send message to transport."""
        if self.is_down():
            return

        if ClientConstants.CANCEL in params:
            return
        _id = self.request_seq
        if kind == MessageKind.REQUEST:
            msg = {
                'id': self.request_seq,
                'method': method,
                'params': params
            }
            self.req_status[self.request_seq] = method
        elif kind == MessageKind.RESPONSE:
            msg = {
                'id': self.request_seq,
                'result': params
            }
        elif kind == MessageKind.NOTIFICATION:
            msg = {
                'method': method,
                'params': params
            }

        logger.debug('{} request: {}'.format(self.language, method))

        # Try sending a message. If the send queue is full, keep trying for a
        # a second before giving up.
        timeout = 1
        start_time = time.time()
        timeout_time = start_time + timeout
        while True:
            try:
                self.zmq_out_socket.send_pyobj(msg, flags=zmq.NOBLOCK)
                self.request_seq += 1
                return int(_id)
            except zmq.error.Again:
                if time.time() > timeout_time:
                    self.sig_went_down.emit(self.language)
                    return
                # The send queue is full! wait 0.1 seconds before retrying.
                if self.initialized:
                    logger.warning("The send queue is full! Retrying...")
                time.sleep(.1)

    @Slot()
    def on_msg_received(self):
        """Process received messages."""
        self.notifier.setEnabled(False)
        while True:
            try:
                # events = self.zmq_in_socket.poll(1500)
                resp = self.zmq_in_socket.recv_pyobj(flags=zmq.NOBLOCK)

                try:
                    method = resp['method']
                    logger.debug(
                        '{} response: {}'.format(self.language, method))
                except KeyError:
                    pass

                if 'error' in resp:
                    logger.debug('{} Response error: {}'
                                 .format(self.language, repr(resp['error'])))
                    if self.language == 'python':
                        # Show PyLS errors in our error report dialog only in
                        # debug or development modes
                        if get_debug_level() > 0 or DEV:
                            message = resp['error'].get('message', '')
                            traceback = (resp['error'].get('data', {}).
                                         get('traceback'))
                            if traceback is not None:
                                traceback = ''.join(traceback)
                                traceback = traceback + '\n' + message
                                self.sig_server_error.emit(traceback)
                        req_id = resp['id']
                        if req_id in self.req_reply:
                            self.req_reply[req_id](None, {'params': []})
                elif 'method' in resp:
                    if resp['method'][0] != '$':
                        if 'id' in resp:
                            self.request_seq = int(resp['id'])
                        if resp['method'] in self.handler_registry:
                            handler_name = (
                                self.handler_registry[resp['method']])
                            handler = getattr(self, handler_name)
                            handler(resp['params'])
                elif 'result' in resp:
                    if resp['result'] is not None:
                        req_id = resp['id']
                        if req_id in self.req_status:
                            req_type = self.req_status[req_id]
                            if req_type in self.handler_registry:
                                handler_name = self.handler_registry[req_type]
                                handler = getattr(self, handler_name)
                                handler(resp['result'], req_id)
                                self.req_status.pop(req_id)
                                if req_id in self.req_reply:
                                    self.req_reply.pop(req_id)
            except RuntimeError:
                # This is triggered when a codeeditor instance has been
                # removed before the response can be processed.
                pass
            except zmq.ZMQError:
                self.notifier.setEnabled(True)
                return

    def perform_request(self, method, params):
        if method in self.sender_registry:
            handler_name = self.sender_registry[method]
            handler = getattr(self, handler_name)
            _id = handler(params)
            if 'response_callback' in params:
                if params['requires_response']:
                    self.req_reply[_id] = params['response_callback']
            return _id

    # ------ LSP initialization methods --------------------------------
    @handles(SERVER_READY)
    @send_request(method=LSPRequestTypes.INITIALIZE)
    def initialize(self, params, *args, **kwargs):
        self.stdio_pid = params['pid']
        pid = self.transport.processId() if not self.external_server else None
        params = {
            'processId': pid,
            'rootUri': pathlib.Path(osp.abspath(self.folder)).as_uri(),
            'capabilities': self.client_capabilites,
            'trace': TRACE
        }
        return params

    @send_request(method=LSPRequestTypes.SHUTDOWN)
    def shutdown(self):
        params = {}
        return params

    @handles(LSPRequestTypes.SHUTDOWN)
    def handle_shutdown(self, response, *args):
        self.ready_to_close = True

    @send_notification(method=LSPRequestTypes.EXIT)
    def exit(self):
        params = {}
        return params

    @handles(LSPRequestTypes.INITIALIZE)
    def process_server_capabilities(self, server_capabilites, *args):
        self.send_plugin_configurations(self.plugin_configurations)
        self.initialized = True
        server_capabilites = server_capabilites['capabilities']

        if isinstance(server_capabilites['textDocumentSync'], int):
            kind = server_capabilites['textDocumentSync']
            server_capabilites['textDocumentSync'] = TEXT_DOCUMENT_SYNC_OPTIONS
            server_capabilites['textDocumentSync']['change'] = kind
        if server_capabilites['textDocumentSync'] is None:
            server_capabilites.pop('textDocumentSync')

        self.server_capabilites.update(server_capabilites)

        self.sig_initialize.emit(self.server_capabilites, self.language)
        self.initialized_call()

    @send_notification(method=LSPRequestTypes.INITIALIZED)
    def initialized_call(self):
        params = {}
        return params


def test():
    """Test LSP client."""
    from spyder.utils.qthelpers import qapplication
    app = qapplication(test_time=8)
    server_args_fmt = '--host %(host)s --port %(port)s --tcp'
    server_settings = {'host': '127.0.0.1', 'port': 2087, 'cmd': 'pyls'}
    lsp = LSPClient(app, server_args_fmt, server_settings)
    lsp.start()

    app.aboutToQuit.connect(lsp.stop)
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    sys.exit(app.exec_())


if __name__ == "__main__":
    test()

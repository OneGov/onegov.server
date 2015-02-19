from __future__ import print_function

import click
import multiprocessing
import os
import signal
import sys

from datetime import datetime
from onegov.server import Server
from onegov.server import Config
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer
from wsgiref.simple_server import make_server, WSGIRequestHandler


class CustomWSGIRequestHandler(WSGIRequestHandler):
    """ Measures the time it takes to respond to a request and prints it
    at the end of the request.

    """

    def parse_request(self):
        self._started = datetime.utcnow()

        return WSGIRequestHandler.parse_request(self)

    def log_request(self, status, bytes):
        duration = datetime.utcnow() - self._started
        duration = int(round(duration.total_seconds() * 1000, 0))

        print("{} - {} {} - {} ms - {} bytes".format(
            status, self.command, self.path, duration, bytes))


class WsgiProcess(multiprocessing.Process):
    """ Runs the WSGI reference server in a separate process. """

    def __init__(self, app_factory, host='127.0.0.1', port=8080):
        multiprocessing.Process.__init__(self)
        self.app_factory = app_factory

        self.host = host

        # if the port is set to 0, a random port will be selected by the os
        self._port = port
        self._actual_port = multiprocessing.Value('i', port)
        self._ready = multiprocessing.Value('i', 0)

        try:
            self.stdin_fileno = sys.stdin.fileno()
        except ValueError:
            pass  # in testing, stdin is not always real

    @property
    def ready(self):
        return self._ready.value == 1

    @property
    def port(self):
        return self._actual_port.value

    def run(self):
        # use the parent's process stdin to be able to provide pdb correctly
        if hasattr(self, 'stdin_fileno'):
            sys.stdin = os.fdopen(self.stdin_fileno)

        # when pressing ctrl+c exit immediately
        signal.signal(signal.SIGINT, lambda *args: sys.exit(0))

        # reset the tty every time, fixing problems that might occur if
        # the process is restarted during a pdb session
        os.system("stty sane")

        server = make_server(
            self.host, self.port, self.app_factory(),
            handler_class=CustomWSGIRequestHandler)

        self._actual_port.value = server.socket.getsockname()[1]
        self._ready.value = 1

        print("started onegov server on https://{}:{}".format(
            self.host, self.port))

        server.serve_forever()


class WsgiServer(FileSystemEventHandler):
    """ Wraps the WSGI process, providing the ability to restart the process
    and acting as an event-handler for watchdog.

    """

    def __init__(self, app_factory, host='127.0.0.1', port=8080):
        self.app_factory = app_factory
        self._host = host
        self._port = port

    def spawn(self):
        return WsgiProcess(self.app_factory, self._host, self._port)

    def join(self, timeout=None):
        if self.process.is_alive():
            self.process.join(timeout)

    def start(self):
        self.process = self.spawn()
        self.process.start()

    def restart(self):
        self.stop()
        self.start()

    def stop(self):
        self.process.terminate()

    def on_any_event(self, event):
        """ If anything of significance changed, restart the process. """

        if event.src_path.endswith('pyc'):
            return

        if '/.git' in event.src_path:
            return

        if '/__pycache__' in event.src_path:
            return

        if '/onegov.server' in event.src_path:
            return

        self.restart()


@click.command()
@click.option(
    '--config-file',
    '-c',
    help="Configuration file to use",
    type=click.Path(exists=True),
    default="onegov.yml"
)
def run(config_file):
    """ Runs the onegov server with the given configuration file in the
    foreground.

    Use this *for debugging/development only*.

    Example::

        onegov-server --config-file test.yml

    The onegov-server will load 'onegov.yml' by default and it will restart
    when any file in the current folder or any of its subfolders changes.
    """

    def wsgi_factory():
        return Server(Config.from_yaml_file(config_file))

    server = WsgiServer(wsgi_factory)
    server.start()

    observer = Observer()
    observer.schedule(server, '.', recursive=True)
    observer.start()

    while True:
        try:
            server.join(1.0)
        except KeyboardInterrupt:
            observer.stop()
            server.stop()
            sys.exit(0)

    observer.join()
    server.join()
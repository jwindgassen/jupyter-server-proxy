import logging
import os
import re
import ssl
from urllib.parse import urlparse

from jupyter_server.utils import ensure_async
from jupyterhub import __version__ as __jh_version__
from jupyterhub.services.auth import HubOAuthCallbackHandler, HubOAuthenticated
from jupyterhub.utils import make_ssl_context
from tornado import httpclient, web, httpserver, ioloop
from tornado.websocket import WebSocketHandler
from traitlets.config import Application, StrDict, ClassesType, KVArgParseConfigLoader
from traitlets.traitlets import Unicode, Int, Bool, default, validate

from .activity import start_activity_update
from ..config import ServerProcess
from ..handlers import SuperviseAndProxyHandler


class StandaloneHubProxyHandler(HubOAuthenticated, SuperviseAndProxyHandler):
    """
    Base class for standalone proxies.
    Will restrict access to the application by authentication with the JupyterHub API.
    """
    environment = {}
    timeout = 60
    skip_authentication = False

    def initialize(self, name, proxy_base, requested_port, requested_unix_socket, mappath, command, environment, timeout, skip_authentication, state):
        super().initialize(state)
        self.name = name
        self.proxy_base = proxy_base
        self.requested_port = requested_port
        self.requested_unix_socket = requested_unix_socket
        self.mappath = mappath
        self.command = command
        self.environment = environment
        self.timeout = timeout
        self.skip_authentication = skip_authentication

    @property
    def hub_users(self):
        if "hub_user" in self.settings:
            return {self.settings["hub_user"]}
        return set()

    @property
    def hub_groups(self):
        if "hub_group" in self.settings:
            return {self.settings["hub_group"]}
        return set()

    def set_default_headers(self):
        self.set_header("X-JupyterHub-Version", __jh_version__)

    def prepare(self, *args, **kwargs):
        pass

    def check_origin(self, origin: str = None):
        # Skip JupyterHandler.check_origin
        return WebSocketHandler.check_origin(self, origin)

    def check_xsrf_cookie(self):
        # Skip HubAuthenticated.check_xsrf_cookie
        pass

    def write_error(self, status_code: int, **kwargs):
        # ToDo: Return proper error page, like in jupyter-server/JupyterHub
        return web.RequestHandler.write_error(self, status_code, **kwargs)

    async def proxy(self, port, path):
        if self.skip_authentication:
            return await super().proxy(port, path)
        else:
            return await ensure_async(self.oauth_proxy(port, path))

    @web.authenticated
    async def oauth_proxy(self, port, path):
        return await super().proxy(port, path)

    def get_env(self):
        return self._render_template(self.environment)

    def get_timeout(self):
        return self.timeout


class StandaloneProxyServer(Application, ServerProcess):
    name = "Standalone Proxy Server"
    description = "A standalone proxy server."

    base_url = Unicode(
        help="""
            Base URL where Requests will be received and proxied. Usually taken from the 
            "JUPYTERHUB_SERVICE_PREFIX" environment variable (or "/" when not set). 
            Set to overwrite.
            
            When setting to "/foo/bar", only incoming requests starting with this prefix will
            be answered by the server and proxied to the proxied app. Any other requests will
            get a 404 response.
            
            Prefixes should not contain a trailing "/", as the JupyterHub will sometimes redirect
            to the URL without a trailing slash. 
        """
    ).tag(config=True)

    @default("prefix")
    def _default_prefix(self):
        return os.environ.get("JUPYTERHUB_SERVICE_PREFIX", "/").removesuffix("/")

    @validate("prefix")
    def _validate_prefix(self, proposal):
        return proposal["value"].removesuffix("/")

    skip_authentication = Bool(
        default=False,
        help="""
            Do not authenticate access to the Server via JupyterHub. When set,
            incoming requests will not be authenticated and anyone can access the
            application.
            
            WARNING: Disabling Authentication can be a major security issue.
        """
    ).tag(config=True)

    address = Unicode(
        help="""
        ToDo
        """
    ).tag(config=True)

    @default("address")
    def _default_address(self):
        if os.environ.get("JUPYTERHUB_SERVICE_URL"):
            url = urlparse(os.environ["JUPYTERHUB_SERVICE_URL"])
            if url.hostname:
                return url.hostname

        return "127.0.0.1"

    port = Int(
        help="""
        ToDo
        """
    ).tag(config=True)

    @default("port")
    def _default_port(self):
        if os.environ.get("JUPYTERHUB_SERVICE_URL"):
            url = urlparse(os.environ["JUPYTERHUB_SERVICE_URL"])

            if url.port:
                return url.port
            elif url.scheme == "http":
                return 80
            elif url.scheme == "https":
                return 443

        return 8889

    server_port = Int(
        default_value=0,
        help=ServerProcess.port.help
    ).tag(config=True)

    activity_interval = Int(
        default_value=300,
        help="""
            Specify an interval to send regulat activity updated to the JupyterHub (in Seconds). 
            When enabled, the Standalone Proxy will try to send a POST request to the JupyterHub API
            containing a timestamp and the name of the server.
            The URL for the activity Endpoint needs to be specified in the "JUPYTERHUB_ACTIVITY_URL"
            environment variable. This URL usually is "/api/users/<user>/activity".
            
            Set to 0 to disable activity notifications.
        """,
    ).tag(config=True)

    websocket_max_message_size = Int(
        default_value=None,
        allow_none=True,
        help="Restrict the size of a message in a WebSocket connection (in Bytes). Tornado defaults to 10MiB."
    ).tag(config=True)

    @default("command")
    def _default_command(self):
        return self.extra_args

    def __init__(self):
        super().__init__()

        # Flags for CLI
        self.flags = {
            **super().flags,
            "absolute_url": (
                {"ServerProcess": {"absolute_url": True}},
                ServerProcess.absolute_url.help
            ),
            "raw_socket_proxy": (
                {"ServerProcess": {"raw_socket_proxy": True}},
                ServerProcess.raw_socket_proxy.help
            ),
            "skip_authentication": (
                {"StandaloneProxyServer": {"skip_authentication": True}},
                self.__class__.skip_authentication.help
            )
        }

        # Create an Alias to all Traits defined in ServerProcess, with some
        # exeptions we do not need, for easier use of the CLI
        # We don't need "command" here, as we will take it from the extra_args
        ignore_traits = ["launcher_entry", "new_browser_tab", "rewrite_response", "update_last_activity", "command"]
        server_process_aliases = {
            trait: f"ServerProcess.{trait}"
            for trait in ServerProcess.class_traits(config=True)
            if trait not in ignore_traits and trait not in self.flags
        }

        self.aliases = {
            **server_process_aliases,
            "address": "StandaloneProxyServer.address",
            "port": "StandaloneProxyServer.port",
            "server_port": "StandaloneProxyServer.server_port",
        }

    def _create_app(self) -> web.Application:
        self.log.debug(f"Process will use {self.port = }")
        self.log.debug(f"Process will use {self.unix_socket = }")
        self.log.debug(f"Process environment: {self.environment}")
        self.log.debug(f"Proxy mappath: {self.mappath}")

        settings = dict(
            debug=self.log_level == logging.DEBUG,
            base_url=self.base_url,
            # Required for JupyterHub
            hub_user=os.environ.get("JUPYTERHUB_USER", ""),
            hub_group=os.environ.get("JUPYTERHUB_GROUP", ""),
            cookie_secret=os.urandom(32),
        )

        if self.websocket_max_message_size:
            self.log.debug(f"Restricting WebSocket Messages to {self.websocket_max_message_size}")
            settings["websocket_max_message_size"] = self.websocket_max_message_size

        base_url = re.escape(self.base_url)
        return web.Application(
            [
                # Redirects from the JupyterHub might not contain a slash
                (f"^{base_url}$", web.RedirectHandler, dict(url=f"{base_url}/")),
                (f"^{base_url}/oauth_callback", HubOAuthCallbackHandler),
                (
                    f"^{base_url}/(.*)",
                    StandaloneHubProxyHandler,
                    dict(
                        name=f"{self.command[0]!r} Process",
                        proxy_base=self.command[0],
                        requested_port=self.server_port,
                        requested_unix_socket=self.unix_socket,
                        mappath=self.mappath,
                        command=self.command,
                        environment=self.environment,
                        timeout=self.timeout,
                        skip_authentication=self.skip_authentication,
                        state={},
                        # ToDo: progressive=progressive
                    ),
                ),
            ],
            **settings,
        )

    def _configure_ssl(self) -> dict | None:
        # See jupyter_server/serverapp:init_webapp
        keyfile = os.environ.get("JUPYTERHUB_SSL_KEYFILE", "")
        certfile = os.environ.get("JUPYTERHUB_SSL_CERTFILE", "")
        client_ca = os.environ.get("JUPYTERHUB_SSL_CLIENT_CA", "")

        if not (keyfile or certfile or client_ca):
            self.log.warn("Could not configure SSL")
            return None

        ssl_options = {}
        if keyfile:
            ssl_options["keyfile"] = keyfile
        if certfile:
            ssl_options["certfile"] = certfile
        if client_ca:
            ssl_options["ca_certs"] = client_ca

        # PROTOCOL_TLS selects the highest ssl/tls protocol version that both the client and
        # server support. When PROTOCOL_TLS is not available use PROTOCOL_SSLv23.
        ssl_options["ssl_version"] = getattr(ssl, "PROTOCOL_TLS", ssl.PROTOCOL_SSLv23)
        if ssl_options.get("ca_certs", False):
            ssl_options["cert_reqs"] = ssl.CERT_REQUIRED

        # Configure HTTPClient to use SSL for Proxy Requests
        ssl_context = make_ssl_context(keyfile, certfile, client_ca)
        httpclient.AsyncHTTPClient.configure(None, defaults={"ssl_options": ssl_context})

        return ssl_options

    def start(self):
        if self.skip_authentication:
            self.log.warn("Disabling Authentication with JuypterHub Server!")

        app = self._create_app()

        ssl_options = self._configure_ssl()
        http_server = httpserver.HTTPServer(app, ssl_options=ssl_options, xheaders=True)
        http_server.listen(self.port, self.address)

        self.log.info(f"Starting standaloneproxy on '{self.address}:{self.port}'")
        self.log.info(f"Base URL: {self.base_url!r}")
        self.log.info(f"Command: {' '.join(self.command)!r}")

        # Periodically send JupyterHub Notifications, that we are still running
        if self.activity_interval > 0:
            self.log.info(
                f"Sending Acitivity Notivication to JupyterHub with interval={self.activity_interval}s"
            )
            start_activity_update(self.activity_interval)

        ioloop.IOLoop.current().start()

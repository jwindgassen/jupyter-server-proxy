import os
import re
from logging import Logger
from urllib.parse import urlparse

from tornado.log import app_log
from tornado.web import Application
from tornado.websocket import WebSocketHandler

from ..handlers import SuperviseAndProxyHandler


class StandaloneProxyHandler(SuperviseAndProxyHandler):
    """
    Base class for standalone proxies. Will not ensure any authentication!
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.environment = {}
        self.timeout = 60

    @property
    def log(self) -> Logger:
        return app_log

    def prepare(self, *args, **kwargs):
        pass

    def check_origin(self, origin: str = None):
        # Skip JupyterHandler.check_origin
        return WebSocketHandler.check_origin(self, origin)

    def get_env(self):
        return self._render_template(self.environment)

    def get_timeout(self):
        return self.timeout


def make_proxy_app(
    destport,
    prefix,
    command,
    use_jupyterhub,
    timeout,
    debug,
    # progressive,
    websocket_max_message_size,
):
    # Determine base class, whether or not to authenticate with JupyterHub
    if use_jupyterhub:
        from .hub import StandaloneHubProxyHandler

        proxy_base = StandaloneHubProxyHandler
    else:
        proxy_base = StandaloneProxyHandler

    # ToDo: environment & mappath
    class Proxy(proxy_base):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.name = command[0]
            self.proxy_base = command[0]
            self.requested_port = destport
            self.mappath = {}
            self.command = command
            self.environment = {}
            self.timeout = timeout

    settings = dict(
        debug=debug,
        # Required for JupyterHub Authentication
        hub_user=os.environ.get("JUPYTERHUB_USER", ""),
        hub_group=os.environ.get("JUPYTERHUB_GROUP", ""),
    )

    if websocket_max_message_size:
        settings["websocket_max_message_size"] = websocket_max_message_size

    app = Application(
        [
            (
                r"^" + re.escape(prefix) + r"/(.*)",
                Proxy,
                dict(
                    state={},
                    # ToDo: progressive=progressive
                ),
            )
        ],
        **settings,
    )

    if use_jupyterhub:
        from jupyterhub.services.auth import HubOAuthCallbackHandler

        # The OAuth Callback required to redirect when we successfully authenticated with JupyterHub
        app.add_handlers(
            ".*",
            [
                (
                    r"^" + re.escape(prefix) + r"/oauth_callback",
                    HubOAuthCallbackHandler,
                ),
            ],
        )

    return app


# https://github.com/jupyterhub/jupyterhub/blob/2.0.0rc3/jupyterhub/singleuser/mixins.py#L340-L349
def get_port_from_env():
    if os.environ.get("JUPYTERHUB_SERVICE_URL"):
        url = urlparse(os.environ["JUPYTERHUB_SERVICE_URL"])
        if url.port:
            return url.port
        elif url.scheme == "http":
            return 80
        elif url.scheme == "https":
            return 443
    return 8888

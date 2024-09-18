import os

from jupyterhub import __version__ as __jh_version__
from jupyterhub.services.auth import HubOAuthenticated
from jupyterhub.utils import make_ssl_context
from tornado import httpclient, web
from tornado.log import app_log as log

from .proxy import StandaloneProxyHandler


class StandaloneHubProxyHandler(HubOAuthenticated, StandaloneProxyHandler):
    """
    Standalone Proxy used when spawned by a JupyterHub.
    Will restrict access to the application by authentication with the JupyterHub API.
    """

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

    @web.authenticated
    async def proxy(self, port, path):
        return await super().proxy(port, path)

    def set_default_headers(self):
        self.set_header("X-JupyterHub-Version", __jh_version__)


def configure_ssl():
    keyfile = os.environ.get("JUPYTERHUB_SSL_KEYFILE")
    certfile = os.environ.get("JUPYTERHUB_SSL_CERTFILE")
    cafile = os.environ.get("JUPYTERHUB_SSL_CLIENT_CA")

    if not (keyfile and certfile and cafile):
        log.warn("Could not configure SSL")
        return None

    ssl_context = make_ssl_context(keyfile, certfile, cafile)

    # Configure HTTPClient to use SSL for Proxy Requests
    httpclient.AsyncHTTPClient.configure(None, defaults={"ssl_options": ssl_context})

    return ssl_context

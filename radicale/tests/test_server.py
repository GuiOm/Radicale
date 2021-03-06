# This file is part of Radicale Server - Calendar Server
# Copyright © 2018 Unrud<unrud@outlook.com>
#
# This library is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Radicale.  If not, see <http://www.gnu.org/licenses/>.

"""
Test the internal server.

"""

import os
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from configparser import ConfigParser
from urllib import request
from urllib.error import HTTPError, URLError

from radicale import config, server

from .helpers import get_file_path

import pytest  # isort:skip

try:
    import gunicorn
except ImportError:
    gunicorn = None


class DisabledRedirectHandler(request.HTTPRedirectHandler):
    def http_error_302(self, req, fp, code, msg, headers):
        raise HTTPError(req.full_url, code, msg, headers, fp)

    http_error_301 = http_error_303 = http_error_307 = http_error_302


class TestBaseServerRequests:
    """Test the internal server."""

    def setup(self):
        self.configuration = config.load()
        self.colpath = tempfile.mkdtemp()
        self.configuration["storage"]["filesystem_folder"] = self.colpath
        # Enable debugging for new processes
        self.configuration["logging"]["level"] = "debug"
        # Disable syncing to disk for better performance
        self.configuration["internal"]["filesystem_fsync"] = "False"
        self.shutdown_socket, shutdown_socket_out = socket.socketpair()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            # Find available port
            sock.bind(("127.0.0.1", 0))
            self.sockname = sock.getsockname()
            self.configuration["server"]["hosts"] = "[%s]:%d" % self.sockname
        self.thread = threading.Thread(target=server.serve, args=(
            self.configuration, shutdown_socket_out))
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
        self.opener = request.build_opener(
            request.HTTPSHandler(context=ssl_context),
            DisabledRedirectHandler)

    def teardown(self):
        self.shutdown_socket.sendall(b" ")
        try:
            self.thread.join()
        except RuntimeError:  # Thread never started
            pass
        shutil.rmtree(self.colpath)

    def request(self, method, path, data=None, is_alive_fn=None, **headers):
        """Send a request."""
        if is_alive_fn is None:
            is_alive_fn = self.thread.is_alive
        scheme = ("https" if self.configuration.getboolean("server", "ssl")
                  else "http")
        req = request.Request(
            "%s://[%s]:%d%s" % (scheme, *self.sockname, path),
            data=data, headers=headers, method=method)
        while True:
            assert is_alive_fn()
            try:
                with self.opener.open(req) as f:
                    return f.getcode(), f.info(), f.read().decode()
            except HTTPError as e:
                return e.code, e.headers, e.read().decode()
            except URLError as e:
                if not isinstance(e.reason, ConnectionRefusedError):
                    raise
            time.sleep(0.1)

    def test_root(self):
        self.thread.start()
        status, _, _ = self.request("GET", "/")
        assert status == 302

    def test_ssl(self):
        self.configuration["server"]["ssl"] = "True"
        self.configuration["server"]["certificate"] = get_file_path("cert.pem")
        self.configuration["server"]["key"] = get_file_path("key.pem")
        self.thread.start()
        status, _, _ = self.request("GET", "/")
        assert status == 302

    @pytest.mark.skipif(not server.HAS_IPV6, reason="IPv6 not supported")
    def test_ipv6(self):
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as sock:
            sock.setsockopt(server.IPPROTO_IPV6, server.IPV6_V6ONLY, 1)
            try:
                # Find available port
                sock.bind(("::1", 0))
            except OSError:
                pytest.skip("IPv6 not supported")
            self.sockname = sock.getsockname()[:2]
            self.configuration["server"]["hosts"] = "[%s]:%d" % self.sockname
        savedEaiAddrfamily = server.EAI_ADDRFAMILY
        if os.name == "nt" and server.EAI_ADDRFAMILY is None:
            # HACK: incomplete errno conversion in WINE
            server.EAI_ADDRFAMILY = -9
        try:
            self.thread.start()
            status, _, _ = self.request("GET", "/")
        finally:
            server.EAI_ADDRFAMILY = savedEaiAddrfamily
        assert status == 302

    def test_command_line_interface(self):
        config_args = []
        for section, values in config.INITIAL_CONFIG.items():
            for option, data in values.items():
                long_name = "--{0}-{1}".format(
                    section, option.replace("_", "-"))
                if data["type"] == bool:
                    if not self.configuration.getboolean(section, option):
                        long_name = "--no{0}".format(long_name[1:])
                    config_args.append(long_name)
                else:
                    config_args.append(long_name)
                    config_args.append(self.configuration.get(section, option))
        env = os.environ.copy()
        env["PYTHONPATH"] = os.pathsep.join(sys.path)
        p = subprocess.Popen(
            [sys.executable, "-m", "radicale"] + config_args, env=env)
        try:
            status, _, _ = self.request(
                "GET", "/", is_alive_fn=lambda: p.poll() is None)
            assert status == 302
        finally:
            p.terminate()
            p.wait()
        if os.name == "posix":
            assert p.returncode == 0

    @pytest.mark.skipif(not gunicorn, reason="gunicorn module not found")
    def test_wsgi_server(self):
        config = ConfigParser()
        config.read_dict(self.configuration)
        assert config.remove_section("internal")
        config_path = os.path.join(self.colpath, "config")
        with open(config_path, "w") as f:
            config.write(f)
        env = os.environ.copy()
        env["PYTHONPATH"] = os.pathsep.join(sys.path)
        p = subprocess.Popen([
            sys.executable,
            "-c", "from gunicorn.app.wsgiapp import run; run()",
            "--bind", self.configuration["server"]["hosts"],
            "--env", "RADICALE_CONFIG=%s" % config_path, "radicale"], env=env)
        try:
            status, _, _ = self.request(
                "GET", "/", is_alive_fn=lambda: p.poll() is None)
            assert status == 302
        finally:
            p.terminate()
            p.wait()
        assert p.returncode == 0

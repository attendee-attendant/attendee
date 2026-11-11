"""A chromedriver-free stand-in for selenium's webdriver.Chrome.

Chrome is launched as a plain subprocess and a force-installed extension does the work that
chromedriver / CDP used to do:
  - a MAIN world content script injected at document_start replaces Page.addScriptToEvaluateOnNewDocument
  - the extension's service worker connects back to us over a websocket and executes commands
    (execute_script, find_element, navigation, cookies, screenshots) using extension APIs.

Only the subset of the selenium API that the Google Meet adapter uses is implemented. Selenium's
pure python helpers (By, WebDriverWait, expected_conditions, exceptions) still work against it.
"""

import base64
import hashlib
import http.server
import itertools
import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time

from selenium.common.exceptions import (
    ElementNotInteractableException,
    JavascriptException,
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
)
from websockets.sync.server import serve

logger = logging.getLogger(__name__)

CHROME_BINARY = "google-chrome"
EXTENSION_TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chrome_extension")

# Selenium Keys that we translate to real X11 key presses
SPECIAL_KEYS = {
    "": "Return",
    "": "Return",
    "": "Tab",
    "": "Escape",
    "": "BackSpace",
}


def extension_id_from_pem(pem_path):
    der = subprocess.run(["openssl", "rsa", "-in", pem_path, "-pubout", "-outform", "DER"], capture_output=True, check=True).stdout
    digest = hashlib.sha256(der).hexdigest()[:32]
    return "".join(chr(ord("a") + int(c, 16)) for c in digest)


class _ProcessHolder:
    def __init__(self, process):
        self.process = process


class ExtensionElement:
    def __init__(self, driver, ref):
        self._driver = driver
        self._ref = ref

    def _el(self, action, **kwargs):
        return self._driver._command("el", payload={"el": self._ref, "action": action, **kwargs})

    def click(self):
        rect = self.rect
        if rect["width"] <= 0 or rect["height"] <= 0:
            raise ElementNotInteractableException("element has zero size")
        self._el("click")

    def send_keys(self, *values):
        text = "".join(str(v) for v in values)
        self._el("focus")
        buffer = ""
        for char in text:
            if char in SPECIAL_KEYS:
                if buffer:
                    self._el("insertText", text=buffer)
                    buffer = ""
                self._driver._x11_key(SPECIAL_KEYS[char])
            else:
                buffer += char
        if buffer:
            self._el("insertText", text=buffer)

    def clear(self):
        self._el("clear")

    def is_displayed(self):
        return bool(self._el("displayed"))

    def is_enabled(self):
        return bool(self._el("enabled"))

    def is_selected(self):
        return bool(self._el("selected"))

    def get_attribute(self, name):
        return self._el("attr", name=name)

    def get_dom_attribute(self, name):
        return self._el("attr", name=name)

    @property
    def text(self):
        return self._el("text")

    @property
    def tag_name(self):
        return self._el("tag")

    @property
    def rect(self):
        return self._el("rect")

    @property
    def size(self):
        r = self.rect
        return {"width": r["width"], "height": r["height"]}

    @property
    def location(self):
        r = self.rect
        return {"x": r["x"], "y": r["y"]}

    def scroll_into_view(self):
        self._el("scroll")

    def find_element(self, by, value):
        return self._driver.find_element(by, value, parent=self)

    def find_elements(self, by, value):
        return self._driver.find_elements(by, value, parent=self)

    def __eq__(self, other):
        return isinstance(other, ExtensionElement) and other._ref == self._ref

    def __hash__(self):
        return hash(self._ref["__el"])


class ExtensionDriver:
    def __init__(self, *, chrome_args, payload_code, host, write_policies_fn, connect_timeout_seconds=60):
        self._host = host or "127.0.0.1"
        self._ids = itertools.count(1)
        self._pending = {}
        self._pending_lock = threading.Lock()
        self._connection = None
        self._connected = threading.Event()
        self._x11_input = None

        self._work_dir = tempfile.mkdtemp(prefix="attendee-ext-")
        self._user_data_dir = os.path.join(self._work_dir, "profile")
        self.capabilities = {"chrome": {"userDataDir": self._user_data_dir}}

        self._start_control_server()
        self._build_extension(payload_code)
        self._start_http_server()

        write_policies_fn(
            {
                "ExtensionSettings": {
                    self.extension_id: {
                        "installation_mode": "force_installed",
                        "update_url": f"http://{self._host}:{self._http_port}/updates.xml",
                    }
                }
            }
        )

        cmd = [CHROME_BINARY, *chrome_args, f"--user-data-dir={self._user_data_dir}", "--no-first-run", "--no-default-browser-check", "about:blank"]
        logger.info(f"Launching chrome: {cmd}")
        self.service = _ProcessHolder(subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True))

        if not self._connected.wait(connect_timeout_seconds):
            raise TimeoutException(f"Chrome extension did not connect within {connect_timeout_seconds}s")
        logger.info(f"Chrome extension {self.extension_id} connected")
        # --start-fullscreen is not reliable without chromedriver. The humanized mouse code needs page coordinates == screen coordinates.
        self._command("fullscreen")

    # ---- setup ----

    def _start_control_server(self):
        def handler(connection):
            self._connection = connection
            self._connected.set()
            try:
                for raw in connection:
                    msg = json.loads(raw)
                    if "id" not in msg:
                        continue
                    with self._pending_lock:
                        entry = self._pending.get(msg["id"])
                    if entry:
                        entry["response"] = msg
                        entry["event"].set()
            except Exception as e:
                logger.info(f"Extension control connection closed: {e}")

        self._control_server = serve(handler, self._host, 0, compression=None, max_size=None)
        self._control_port = self._control_server.socket.getsockname()[1]
        threading.Thread(target=self._control_server.serve_forever, daemon=True, name="extension-control-server").start()
        logger.info(f"Extension control server listening on ws://{self._host}:{self._control_port}")

    def _build_extension(self, payload_code):
        ext_dir = os.path.join(self._work_dir, "ext")
        shutil.copytree(EXTENSION_TEMPLATE_DIR, ext_dir)
        with open(os.path.join(ext_dir, "payload.js"), "w") as f:
            f.write(payload_code)
        with open(os.path.join(ext_dir, "config.js"), "w") as f:
            f.write(f"self.ATTENDEE_CONTROL_URL = {json.dumps(f'ws://{self._host}:{self._control_port}')};\n")

        pack_profile = os.path.join(self._work_dir, "pack-profile")
        subprocess.run(
            [CHROME_BINARY, "--no-sandbox", f"--user-data-dir={pack_profile}", f"--pack-extension={ext_dir}"],
            capture_output=True,
            timeout=60,
        )
        self._crx_path = ext_dir + ".crx"
        if not os.path.exists(self._crx_path):
            raise RuntimeError("Failed to pack chrome extension")
        self.extension_id = extension_id_from_pem(ext_dir + ".pem")
        logger.info(f"Packed chrome extension {self.extension_id}")

    def _start_http_server(self):
        driver = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path.startswith("/updates.xml"):
                    body = (f"<?xml version='1.0' encoding='UTF-8'?><gupdate xmlns='http://www.google.com/update2/response' protocol='2.0'><app appid='{driver.extension_id}'><updatecheck codebase='http://{driver._host}:{driver._http_port}/ext.crx' version='1.0' /></app></gupdate>").encode()
                    content_type = "application/xml"
                elif self.path.startswith("/ext.crx"):
                    with open(driver._crx_path, "rb") as f:
                        body = f.read()
                    content_type = "application/x-chrome-extension"
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format, *args):
                logger.info("extension http server: " + format % args)

        self._http_server = http.server.ThreadingHTTPServer((self._host, 0), Handler)
        self._http_port = self._http_server.server_address[1]
        threading.Thread(target=self._http_server.serve_forever, daemon=True, name="extension-http-server").start()

    # ---- command channel ----

    def _command(self, cmd, timeout=90, **kwargs):
        if self._connection is None:
            raise JavascriptException("Extension not connected")
        msg_id = next(self._ids)
        entry = {"event": threading.Event(), "response": None}
        with self._pending_lock:
            self._pending[msg_id] = entry
        try:
            self._connection.send(json.dumps({"id": msg_id, "cmd": cmd, **kwargs}))
            if not entry["event"].wait(timeout):
                raise TimeoutException(f"Extension command {cmd} timed out")
        finally:
            with self._pending_lock:
                self._pending.pop(msg_id, None)

        response = entry["response"]
        if response.get("ok"):
            return self._wrap(response.get("value"))
        error = response.get("error")
        message = response.get("message")
        if error == "stale":
            raise StaleElementReferenceException(message)
        raise JavascriptException(f"{cmd} failed: {message}")

    def _wrap(self, value):
        if isinstance(value, list):
            return [self._wrap(v) for v in value]
        if isinstance(value, dict):
            if "__el" in value and len(value) == 1:
                return ExtensionElement(self, value)
            return {k: self._wrap(v) for k, v in value.items()}
        return value

    def _unwrap(self, value):
        if isinstance(value, ExtensionElement):
            return value._ref
        if isinstance(value, (list, tuple)):
            return [self._unwrap(v) for v in value]
        if isinstance(value, dict):
            return {k: self._unwrap(v) for k, v in value.items()}
        return value

    def _x11_key(self, key_name):
        if self._x11_input is None:
            from bots.web_bot_adapter.x11_input import X11Input

            self._x11_input = X11Input()
        self._x11_input.key_press(key_name)
        self._x11_input.key_release(key_name)

    # ---- selenium-ish API ----

    def execute_script(self, script, *args):
        return self._command("exec", payload={"script": script, "args": self._unwrap(list(args))})

    def find_elements(self, by, value, parent=None):
        return self._command("find", payload={"by": by, "value": value, "parent": parent._ref if parent else None})

    def find_element(self, by, value, parent=None):
        elements = self.find_elements(by, value, parent=parent)
        if not elements:
            raise NoSuchElementException(f"no element for {by}={value}")
        return elements[0]

    def get(self, url):
        self._command("get", url=url)

    @property
    def current_url(self):
        return self._command("current_url")

    def save_screenshot(self, path):
        data = self._command("screenshot")
        with open(path, "wb") as f:
            f.write(base64.b64decode(data))
        return True

    def get_cookies(self):
        return self._command("get_cookies")

    def add_cookie(self, cookie):
        self._command("add_cookie", cookie=cookie)

    def delete_all_cookies(self):
        self._command("delete_all_cookies")

    def send_key(self, key_name):
        self._x11_key(key_name)

    def execute_cdp_cmd(self, cmd, params):
        # No CDP. Emulate the few commands the adapter uses.
        if cmd == "Page.captureSnapshot":
            return {"data": self._command("mhtml")}
        if cmd == "Page.getNavigationHistory":
            return {"currentIndex": 0, "entries": [{"url": self.current_url}]}
        if cmd == "Runtime.evaluate":
            return {"result": {"value": self.execute_script("return " + params["expression"])}}
        logger.warning(f"execute_cdp_cmd({cmd}) is not supported by ExtensionDriver, ignoring")
        return {}

    def close(self):
        pass

    def quit(self):
        for server in (self._control_server, self._http_server):
            try:
                server.shutdown()
            except Exception:
                pass
        process = self.service.process
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
        time.sleep(0.5)
        shutil.rmtree(self._work_dir, ignore_errors=True)

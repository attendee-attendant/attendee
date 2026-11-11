"""A webdriver-free stand-in for selenium's webdriver, backed by Camoufox and a WebExtension.

Camoufox (an anti-fingerprinting Firefox fork) is launched as a plain subprocess, not through Playwright / juggler.
We only use the camoufox python package to generate a fingerprint (passed to the browser as CAMOU_CONFIG_* env vars)
and to have Camoufox install our unpacked extension as a temporary addon at startup. The extension does the work that chromedriver / CDP used to do:
  - a MAIN world content script injected at document_start replaces Page.addScriptToEvaluateOnNewDocument
  - the extension's background page connects back to us over a websocket and executes commands
    (execute_script, find_element, navigation, cookies, screenshots) using extension APIs.

Only the subset of the selenium API that the Google Meet adapter uses is implemented. Selenium's
pure python helpers (By, WebDriverWait, expected_conditions, exceptions) still work against it.
"""

import base64
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

CAMOUFOX_BINARY = os.environ.get("CAMOUFOX_EXECUTABLE_PATH", "/opt/camoufox/camoufox-bin")
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
EXTENSION_TEMPLATE_DIR = os.path.join(CURRENT_DIR, "chrome_extension")
FIREFOX_POLYFILLS_PATH = os.path.join(CURRENT_DIR, "firefox_polyfills.js")
EXTENSION_ID = "attendee-bot@attendee.dev"

# Selenium Keys that we translate to real X11 key presses
SPECIAL_KEYS = {
    "\ue006": "Return",
    "\ue007": "Return",
    "\ue004": "Tab",
    "\ue00c": "Escape",
    "\ue003": "BackSpace",
}

FIREFOX_PREFS = {
    # Never suspend the extension's background page (it holds the control websocket)
    "extensions.background.idle.enabled": False,
    # Equivalents of --use-fake-device-for-media-stream / --use-fake-ui-for-media-stream / --autoplay-policy
    "media.navigator.streams.fake": True,
    "media.navigator.permission.disabled": True,
    "media.autoplay.default": 0,
    "media.autoplay.blocking_policy": 0,
    # No first-run / UI noise that could overlay the page
    "browser.shell.checkDefaultBrowser": False,
    "browser.startup.homepage_override.mstone": "ignore",
    "browser.aboutwelcome.enabled": False,
    # Skip the "Welcome to Firefox" terms of use modal, which covers the page
    "termsofuse.bypassNotification": True,
    "termsofuse.acceptedVersion": 999,
    "termsofuse.acceptedDate": "1700000000000",
    "datareporting.policy.dataSubmissionPolicyBypassNotification": True,
    "browser.preonboarding.enabled": False,
    # Disable onboarding messages, e.g. the "We've updated our Privacy Notice" infobar that shifts the page down
    "browser.newtabpage.activity-stream.asrouter.providers.onboarding": json.dumps({"id": "onboarding", "type": "local", "localProvider": "OnboardingMessageProvider", "enabled": False, "exclude": []}),
    "datareporting.policy.dataSubmissionEnabled": False,
    "toolkit.telemetry.reportingpolicy.firstRun": False,
    "browser.translations.automaticallyPopup": False,
    "signon.rememberSignons": False,
    "full-screen-api.warning.timeout": 0,
    "privacy.webrtc.hideGlobalIndicator": True,
    "app.update.enabled": False,
    # Grant MV3 host permissions (<all_urls>) at install, instead of leaving them optional
    "extensions.originControls.grantByDefault": True,
    # Local network access checks would block the extension / page websockets to our 127.x.x.x servers
    "network.lna.enabled": False,
    "network.lna.blocking": False,
    # The Meet page (https) talks to our ws://127.x.x.x server
    "network.websocket.allowInsecureFromHTTPS": True,
}


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
    def __init__(self, *, browser_args, window_size, payload_code, host, connect_timeout_seconds=60):
        self._host = host or "127.0.0.1"
        self._ids = itertools.count(1)
        self._pending = {}
        self._pending_lock = threading.Lock()
        self._connection = None
        self._connected = threading.Event()
        self._x11_input = None

        self._work_dir = tempfile.mkdtemp(prefix="attendee-ext-")
        self._profile_dir = os.path.join(self._work_dir, "profile")
        self._extension_dir = os.path.join(self._work_dir, "extension")
        # teardown_driver pkills by this path
        self.capabilities = {"chrome": {"userDataDir": self._profile_dir}}

        self._start_control_server()
        self._build_profile(payload_code)

        # Generates a fingerprint (CAMOU_CONFIG_*), per identity prefs (CAMOU_PREFS_*) and the addon list.
        # It briefly launches the browser to probe the host GPU, so it needs DISPLAY to be set.
        from camoufox.addons import DefaultAddons
        from camoufox.utils import launch_options

        options = launch_options(
            executable_path=CAMOUFOX_BINARY,
            os="linux",
            window=window_size,
            headless=False,
            addons=[self._extension_dir],
            exclude_addons=[DefaultAddons.UBO],
            i_know_what_im_doing=True,
        )

        # Firefox's sandbox falls back to seccomp-only when it can't create user namespaces (e.g. in docker),
        # so we leave it on. Disabling it shows a "security sandbox is disabled" infobar that shifts the page.
        cmd = [options["executable_path"], *options["args"], *browser_args, "--profile", self._profile_dir, "--no-remote", "about:blank"]
        logger.info(f"Launching camoufox: {cmd}")
        self.service = _ProcessHolder(subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True, env=options["env"]))

        if not self._connected.wait(connect_timeout_seconds):
            raise TimeoutException(f"Camoufox extension did not connect within {connect_timeout_seconds}s")
        logger.info("Camoufox extension connected")

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

    def _build_profile(self, payload_code):
        with open(FIREFOX_POLYFILLS_PATH, "r") as f:
            polyfills = f.read()

        files = {
            "payload.js": polyfills + "\n" + payload_code,
            "config.js": f"self.ATTENDEE_CONTROL_URL = {json.dumps(f'ws://{self._host}:{self._control_port}')};\n",
        }
        for name in os.listdir(EXTENSION_TEMPLATE_DIR):
            with open(os.path.join(EXTENSION_TEMPLATE_DIR, name), "r") as f:
                files[name] = f.read()

        # Unpacked extension, which Camoufox installs as a temporary addon (no signing needed)
        os.makedirs(self._extension_dir)
        for name, content in files.items():
            with open(os.path.join(self._extension_dir, name), "w") as f:
                f.write(content)

        os.makedirs(self._profile_dir)

        with open(os.path.join(self._profile_dir, "user.js"), "w") as f:
            for key, value in FIREFOX_PREFS.items():
                f.write(f"user_pref({json.dumps(key)}, {json.dumps(value)});\n")

        logger.info(f"Built camoufox profile with extension at {self._profile_dir}")

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
            # Firefox has no MHTML capture API
            return {"data": ""}
        if cmd == "Page.getNavigationHistory":
            return {"currentIndex": 0, "entries": [{"url": self.current_url}]}
        if cmd == "Runtime.evaluate":
            return {"result": {"value": self.execute_script("return " + params["expression"])}}
        logger.warning(f"execute_cdp_cmd({cmd}) is not supported by ExtensionDriver, ignoring")
        return {}

    def close(self):
        pass

    def quit(self):
        for server in (self._control_server,):
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

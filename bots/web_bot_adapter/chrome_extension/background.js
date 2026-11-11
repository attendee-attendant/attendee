// Control channel between the Python bot process and the browser.
// Replaces chromedriver / CDP: Python sends commands over a websocket, we execute them with extension APIs.
// config.js (listed before this file in the manifest) defines self.ATTENDEE_CONTROL_URL

// Runs in the page's MAIN world. Must be self contained, because chrome.scripting serializes it.
function pageOp(op, payload) {
  const els = (window.__attendeeDriverEls ||= { map: new Map(), next: 1 });

  const ref = (el) => {
    for (const [id, existing] of els.map) {
      if (existing === el) return { __el: id };
    }
    const id = String(els.next++);
    els.map.set(id, el);
    return { __el: id };
  };

  const deref = (r) => {
    const el = els.map.get(r.__el);
    if (!el || !el.isConnected) throw { __error: "stale", message: "stale element reference" };
    return el;
  };

  const decode = (v) => {
    if (Array.isArray(v)) return v.map(decode);
    if (v && typeof v === "object") {
      if (v.__el) return deref(v);
      const out = {};
      for (const k of Object.keys(v)) out[k] = decode(v[k]);
      return out;
    }
    return v;
  };

  const encode = (v, depth = 0) => {
    if (depth > 20) return null;
    if (v === undefined || v === null) return null;
    if (v instanceof Element) return ref(v);
    if (Array.isArray(v) || v instanceof NodeList || v instanceof HTMLCollection) return Array.from(v, (x) => encode(x, depth + 1));
    if (typeof v === "object") {
      const out = {};
      for (const k of Object.keys(v)) out[k] = encode(v[k], depth + 1);
      return out;
    }
    if (typeof v === "function") return null;
    return v;
  };

  const find = (root, by, value) => {
    switch (by) {
      case "xpath": {
        const res = document.evaluate(value, root, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
        const out = [];
        for (let i = 0; i < res.snapshotLength; i++) out.push(res.snapshotItem(i));
        return out;
      }
      case "css selector":
        return Array.from(root.querySelectorAll(value));
      case "tag name":
        return Array.from(root.getElementsByTagName(value));
      case "id":
        return Array.from(root.querySelectorAll(`#${CSS.escape(value)}`));
      case "name":
        return Array.from(root.querySelectorAll(`[name="${CSS.escape(value)}"]`));
      case "class name":
        return Array.from(root.getElementsByClassName(value));
      default:
        throw { __error: "js", message: `unsupported locator ${by}` };
    }
  };

  const isDisplayed = (el) => {
    if (!el.isConnected) return false;
    const style = getComputedStyle(el);
    if (style.visibility === "hidden" || style.display === "none" || Number(style.opacity) === 0) return false;
    return el.getClientRects().length > 0;
  };

  const run = async () => {
    switch (op) {
      case "exec": {
        // Meet enforces trusted types, so eval needs a TrustedScript
        if (!window.__attendeeDriverTT) {
          window.__attendeeDriverTT = window.trustedTypes ? window.trustedTypes.createPolicy("attendee-driver", { createScript: (s) => s }) : { createScript: (s) => s };
        }
        const fn = (0, eval)(window.__attendeeDriverTT.createScript(`(function(){${payload.script}\n})`));
        return encode(await fn.apply(window, decode(payload.args || [])));
      }
      case "find": {
        const root = payload.parent ? deref(payload.parent) : document;
        return find(root, payload.by, payload.value).map(ref);
      }
      case "el": {
        const el = deref(payload.el);
        switch (payload.action) {
          case "click":
            el.scrollIntoView({ block: "center", inline: "center" });
            el.click();
            return null;
          case "focus":
            el.scrollIntoView({ block: "center", inline: "center" });
            el.focus();
            return null;
          case "clear":
            el.value = "";
            el.dispatchEvent(new Event("input", { bubbles: true }));
            return null;
          case "insertText":
            el.focus();
            document.execCommand("insertText", false, payload.text);
            return null;
          case "displayed":
            return isDisplayed(el);
          case "enabled":
            return !el.disabled;
          case "selected":
            return !!(el.checked || el.selected);
          case "rect": {
            const r = el.getBoundingClientRect();
            return { x: r.left, y: r.top, width: r.width, height: r.height };
          }
          case "attr": {
            const prop = el[payload.name];
            if (prop !== undefined && prop !== null && typeof prop !== "object" && typeof prop !== "function") return String(prop);
            return el.getAttribute(payload.name);
          }
          case "text":
            return el.innerText;
          case "tag":
            return el.tagName.toLowerCase();
          case "scroll":
            el.scrollIntoView({ block: "center", inline: "center" });
            return null;
          default:
            throw { __error: "js", message: `unknown element action ${payload.action}` };
        }
      }
      default:
        throw { __error: "js", message: `unknown op ${op}` };
    }
  };

  return run().then(
    (value) => ({ ok: true, value }),
    (e) => ({ ok: false, error: e && e.__error ? e.__error : "js", message: e && e.message ? String(e.message) : String(e) })
  );
}

async function getTab() {
  const tabs = await chrome.tabs.query({ windowType: "normal" });
  if (!tabs.length) throw new Error("no tab");
  return tabs.find((t) => t.active) || tabs[0];
}

function waitForTabComplete(tabId, timeoutMs = 60000) {
  return new Promise((resolve) => {
    const timer = setTimeout(done, timeoutMs);
    function done() {
      clearTimeout(timer);
      chrome.tabs.onUpdated.removeListener(listener);
      resolve();
    }
    function listener(id, info) {
      if (id === tabId && info.status === "complete") done();
    }
    chrome.tabs.onUpdated.addListener(listener);
  });
}

async function runInPage(op, payload) {
  const tab = await getTab();
  const [injection] = await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    world: "MAIN",
    func: pageOp,
    args: [op, payload],
  });
  const result = injection && injection.result;
  if (!result) return { ok: false, error: "js", message: "no result from page" };
  return result;
}

async function handleCommand(msg) {
  switch (msg.cmd) {
    case "exec":
    case "find":
    case "el":
      return runInPage(msg.cmd, msg.payload);
    case "get": {
      const tab = await getTab();
      const done = waitForTabComplete(tab.id);
      await chrome.tabs.update(tab.id, { url: msg.url });
      await done;
      return { ok: true, value: null };
    }
    case "current_url": {
      const tab = await getTab();
      return { ok: true, value: tab.pendingUrl || tab.url };
    }
    case "screenshot": {
      const tab = await getTab();
      const dataUrl = await chrome.tabs.captureVisibleTab(tab.windowId, { format: "png" });
      return { ok: true, value: dataUrl.split(",")[1] };
    }
    case "get_cookies": {
      const tab = await getTab();
      const cookies = await chrome.cookies.getAll({ url: tab.url });
      return { ok: true, value: cookies };
    }
    case "add_cookie": {
      const tab = await getTab();
      const c = msg.cookie;
      const host = (c.domain || new URL(tab.url).hostname).replace(/^\./, "");
      const details = {
        url: `${c.secure === false ? "http" : "https"}://${host}${c.path || "/"}`,
        name: c.name,
        value: c.value,
        path: c.path || "/",
        secure: c.secure !== false,
        httpOnly: !!c.httpOnly,
      };
      if (c.domain) details.domain = c.domain;
      if (c.expiry) details.expirationDate = c.expiry;
      if (c.sameSite) details.sameSite = { Strict: "strict", Lax: "lax", None: "no_restriction" }[c.sameSite] || "unspecified";
      await chrome.cookies.set(details);
      return { ok: true, value: null };
    }
    case "delete_all_cookies": {
      const cookies = await chrome.cookies.getAll({});
      await Promise.all(cookies.map((c) => chrome.cookies.remove({ url: `${c.secure ? "https" : "http"}://${c.domain.replace(/^\./, "")}${c.path}`, name: c.name, storeId: c.storeId })));
      return { ok: true, value: null };
    }
    case "fullscreen": {
      const tab = await getTab();
      await chrome.windows.update(tab.windowId, { state: "fullscreen" });
      return { ok: true, value: null };
    }
    case "ping":
      return { ok: true, value: "pong" };
    default:
      return { ok: false, error: "js", message: `unknown command ${msg.cmd}` };
  }
}

let socket = null;

function connect() {
  socket = new WebSocket(self.ATTENDEE_CONTROL_URL);
  socket.onmessage = async (event) => {
    const msg = JSON.parse(event.data);
    let response;
    try {
      response = await handleCommand(msg);
    } catch (e) {
      response = { ok: false, error: "js", message: String(e && e.message ? e.message : e) };
    }
    socket.send(JSON.stringify({ id: msg.id, ...response }));
  };
  socket.onclose = () => setTimeout(connect, 500);
  socket.onerror = () => {};
}

// Keepalive (the bot also sets extensions.background.idle.enabled=false so the event page is never suspended)
setInterval(() => {
  if (socket && socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ keepalive: true }));
}, 20000);

connect();

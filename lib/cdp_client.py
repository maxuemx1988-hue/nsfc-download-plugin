"""
Chrome DevTools Protocol (CDP) WebSocket client.
"""
import json
import time
import websocket
import requests

from .config_loader import CDP_PORT, NSFC_HOST


class CDPClient:
    def __init__(self, port=CDP_PORT):
        self.port = port
        self.ws = None
        self._id = 0
        self._xhr_intercepted = False
        self._saved_tab = None

    def inject_xhr_interceptor(self):
        """Inject XHR interceptor to capture API responses."""
        if self._xhr_intercepted:
            return
        self.evaluate_js("""
        (() => {
            window._lastApiResponse = null;
            window._apiResponsePromise = null;

            const origSend = XMLHttpRequest.prototype.send;
            XMLHttpRequest.prototype.send = function(body) {
                const self = this;
                const origOnReadyStateChange = this.onreadystatechange;
                const origOnLoad = this.onload;

                this.addEventListener('load', function() {
                    try {
                        if (this.responseType === '' || this.responseType === 'text') {
                            window._lastApiResponse = {
                                url: self._apiUrl || '',
                                status: this.status,
                                text: this.responseText,
                            };
                            if (window._apiResponseResolve) {
                                const resolve = window._apiResponseResolve;
                                window._apiResponseResolve = null;
                                resolve(window._lastApiResponse);
                            }
                        }
                    } catch(e) {}
                });

                return origSend.apply(this, arguments);
            };

            const origOpen = XMLHttpRequest.prototype.open;
            XMLHttpRequest.prototype.open = function(method, url) {
                this._apiUrl = url;
                return origOpen.apply(this, arguments);
            };

            window._xhrInterceptorReady = true;
        })()
        """)
        self._xhr_intercepted = True

    def wait_for_api_response(self, timeout=5):
        """Wait for the next XHR response."""
        self.evaluate_js("window._lastApiResponse = null;")
        start = time.time()
        while time.time() - start < timeout:
            result = self.evaluate_js("window._lastApiResponse")
            val = result.get("result", {}).get("value")
            if val and val != "null":
                return val
            time.sleep(0.2)
        return None

    def get_browser_info(self):
        resp = requests.get(f"http://127.0.0.1:{self.port}/json/version", timeout=5)
        return resp.json()

    def get_tabs(self):
        resp = requests.get(f"http://127.0.0.1:{self.port}/json", timeout=5)
        return resp.json()

    def find_tab(self, keyword=None):
        tabs = self.get_tabs()
        for tab in tabs:
            url = tab.get("url", "")
            if keyword and keyword in url:
                return tab
        for tab in tabs:
            if tab.get("type") == "page" and not tab.get("url", "").startswith("devtools://"):
                return tab
        return None

    def connect(self, tab=None):
        if tab is None:
            tab = self.find_tab("finalSearchList")
            if tab is None:
                tab = self.find_tab("kd.nsfc.cn")
        if tab is None:
            raise RuntimeError("No suitable tab found")

        ws_url = tab.get("webSocketDebuggerUrl")
        if not ws_url:
            raise RuntimeError(f"No WebSocket URL for tab: {tab}")

        print(f"CDP connected: {ws_url[:60]}...")
        self.ws = websocket.create_connection(
            ws_url, timeout=30,
            origin="http://127.0.0.1",
            suppress_origin=True
        )
        self._id = 0
        self._saved_tab = tab
        return tab

    def reconnect(self):
        print("  CDP reconnecting...")
        self.close()
        time.sleep(1)
        tab = self.find_tab("finalSearchList")
        if tab is None:
            tab = self.find_tab("kd.nsfc.cn")
        if tab is None:
            raise RuntimeError("No suitable tab for reconnect")
        ws_url = tab.get("webSocketDebuggerUrl")
        if not ws_url:
            raise RuntimeError("No WebSocket URL for reconnect tab")
        self.ws = websocket.create_connection(
            ws_url, timeout=30,
            origin="http://127.0.0.1",
            suppress_origin=True
        )
        self._id = 0
        self._saved_tab = tab
        return tab

    def send(self, method, params=None):
        self._id += 1
        cmd = {"id": self._id, "method": method}
        if params:
            cmd["params"] = params
        self.ws.send(json.dumps(cmd))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self._id:
                if "result" in msg:
                    return msg["result"]
                elif "error" in msg:
                    raise RuntimeError(f"CDP error: {msg['error']}")
        return None

    def evaluate_js(self, expression):
        return self.send("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": True
        })

    def navigate(self, url):
        return self.send("Page.navigate", {"url": url})

    def wait_for_load(self, timeout=30):
        self.send("Page.enable")
        start = time.time()
        while time.time() - start < timeout:
            result = self.evaluate_js("document.readyState")
            if result.get("result", {}).get("value") == "complete":
                return True
            time.sleep(0.5)
        return False

    def get_cookies(self, domain=None):
        params = {}
        if domain:
            params["urls"] = [f"https://{domain}"]
        result = self.send("Network.getCookies", params)
        return result.get("cookies", [])

    def close(self):
        if self.ws:
            self.ws.close()
            self.ws = None


def call_nsfc_api_via_js(client, api_path, data=None, method="POST"):
    """Call NSFC API through browser JS (cookies carried automatically)."""
    body = ""
    if data:
        body = "&".join(f"{k}={v}" for k, v in data.items())

    js = f"""
    (async () => {{
        try {{
            const resp = await fetch('{NSFC_HOST}{api_path}', {{
                method: '{method}',
                headers: {{
                    'accept': 'application/json, text/plain, */*',
                    'content-type': 'application/x-www-form-urlencoded',
                    'origin': '{NSFC_HOST}',
                    'referer': '{NSFC_HOST}/'
                }},
                body: {json.dumps(body) if body else 'null'}
            }});
            const text = await resp.text();
            return text;
        }} catch (e) {{
            return JSON.stringify({{error: e.message}});
        }}
    }})()
    """
    result = client.evaluate_js(js)
    value = result.get("result", {}).get("value", "")
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return {"raw": value}

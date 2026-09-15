"""Real Chrome regression checks against the isolated simulated HTTP server."""
import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
from urllib.request import urlopen

import pytest
from websockets.sync.client import connect

from tests.test_web_server import srv


class Browser:
    def __init__(self, socket):
        self.socket, self.sequence = socket, 0

    def call(self, method, params=None):
        self.sequence += 1
        self.socket.send(json.dumps({"id": self.sequence, "method": method, "params": params or {}}))
        while True:
            message = json.loads(self.socket.recv(timeout=20))
            if message.get("id") == self.sequence:
                assert "error" not in message, message
                return message.get("result", {})

    def evaluate(self, expression):
        result = self.call("Runtime.evaluate", {"expression": expression, "returnByValue": True, "awaitPromise": True})
        assert not result.get("exceptionDetails"), result
        return result["result"].get("value")

    def wait(self, expression, timeout=20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.evaluate(expression):
                return
            time.sleep(.1)
        pytest.fail(f"Browser condition timed out: {expression}; " + str(self.evaluate("document.body.innerText")))

    def screenshot(self, path):
        path.write_bytes(base64.b64decode(self.call("Page.captureScreenshot", {"format": "png"})["data"]))


@pytest.fixture
def browser(tmp_path):
    binary = os.environ.get("CHROME_BIN") or shutil.which("google-chrome") or shutil.which("chromium")
    if not binary:
        mac = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        if mac.exists():
            binary = str(mac)
    if not binary:
        pytest.skip("Chrome/Chromium is required for browser interaction tests")
    profile = tmp_path / "chrome"
    log = (tmp_path / "chrome.log").open("w")
    process = subprocess.Popen([binary, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
                                f"--user-data-dir={profile}", "--remote-debugging-port=0", "about:blank"],
                               stdout=subprocess.DEVNULL, stderr=log)
    try:
        port_file = profile / "DevToolsActivePort"
        deadline = time.monotonic() + 45
        while not port_file.exists() and time.monotonic() < deadline:
            if process.poll() is not None:
                pytest.fail(f"Chrome exited before opening its debugging port; see {log.name}")
            time.sleep(.1)
        assert port_file.exists(), f"Chrome did not open its debugging port within 45 seconds; see {log.name}"
        port = port_file.read_text().splitlines()[0]
        with urlopen(f"http://127.0.0.1:{port}/json/list", timeout=5) as response:
            target = next(t for t in json.load(response) if t["type"] == "page")
        with connect(target["webSocketDebuggerUrl"]) as socket:
            page = Browser(socket)
            page.call("Page.enable")
            page.call("Emulation.setFocusEmulationEnabled", {"enabled": True})
            page.call("Emulation.setDeviceMetricsOverride", {"width": 1200, "height": 1100,
                                                            "deviceScaleFactor": 1, "mobile": False})
            yield page
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        log.close()


def open_form(browser, base):
    browser.call("Page.navigate", {"url": base})
    browser.wait('typeof state !== "undefined" && !!document.getElementById("group-count-0")')
    assert browser.evaluate("state.demo") is True
    browser.evaluate('''function change(id,value,kind="input"){
      const el=document.getElementById(id);el.value=value;el.dispatchEvent(new Event(kind,{bubbles:true}));
    }''')


def test_loading_list_updates_in_place_without_losing_typed_models_or_counts(browser, srv, monkeypatch):
    base, manager = srv
    gate = threading.Event()
    original = manager.api.options
    def delayed(*args, **kwargs):
        assert gate.wait(20)
        data = original(*args, **kwargs)
        # A substring match such as "metal" must not precede the requested family.
        return {**data, "instance_types": [{"type": "c5.metal"}, *data["instance_types"]]}
    monkeypatch.setattr(manager.api, "options", delayed)
    try:
        open_form(browser, base)
        browser.evaluate('change("group-count-0","2");change("group-keep-0","2");document.getElementById("add-group").click();change("group-type-1","t")')
        assert browser.evaluate('document.querySelector("#group-type-1-combo .combo-menu").textContent.includes("正在加载")')
        assert not browser.evaluate('document.querySelector("#group-type-1-combo .combo-menu").textContent.includes("0 / 0")')
        gate.set()
        browser.wait('!state.loading && document.querySelectorAll("#group-type-1-list [role=option]").length > 0')
        assert browser.evaluate('document.getElementById("group-type-1").value') == "t"
        assert browser.evaluate("state.form.instance_groups[0].count") == 2
        assert browser.evaluate("state.form.keep_top_k") == 2
        assert browser.evaluate('document.querySelector("#group-type-1-list [role=option] strong").textContent').startswith("t")
        browser.evaluate('document.querySelectorAll("#group-type-1-list [role=option]")[0].click()')
        assert browser.evaluate("state.form.instance_groups[1].instance_type").startswith("t")
    finally:
        gate.set()


def test_failed_catalog_retries_and_does_not_reuse_another_regions_list(browser, srv, monkeypatch):
    base, manager = srv
    original = manager.api.options
    fail = [True]
    def response(*args, **kwargs):
        data = original(*args, **kwargs)
        return {**data, "instance_types": [], "errors": ["测试：无法连接 AWS"], "instance_types_source": "unavailable"} if fail[0] else data
    monkeypatch.setattr(manager.api, "options", response)
    open_form(browser, base)
    browser.wait('!state.loading && !!state.optionsError')
    browser.evaluate('document.getElementById("group-type-0").focus();change("group-type-0","t")')
    assert browser.evaluate('document.querySelector("#group-type-0-combo .combo-menu").textContent.includes("加载失败")')
    fail[0] = False
    browser.evaluate('document.getElementById("reload-options").click()')
    browser.wait('!state.loading && state.options.instance_types.length > 0')
    assert browser.evaluate('document.getElementById("group-type-0").value') == "t"
    fail[0] = True
    browser.evaluate('document.getElementById("reload-options").click()')
    browser.wait('!state.loading && !!state.optionsError')
    assert browser.evaluate("state.options.instance_types.length") > 0
    browser.evaluate('change("region-input","us-west-2","change")')
    browser.wait('!state.loading && !!state.optionsError')
    assert browser.evaluate("state.options.instance_types.length") == 0


def test_per_round_and_final_retention_are_visible_and_used_by_simulated_run(browser, srv, tmp_path):
    base, _ = srv
    open_form(browser, base)
    browser.wait("!state.loading && state.imageStatus === 'ready'")
    assert browser.evaluate('document.getElementById("group-keep-0").closest("details") === null')
    browser.evaluate('change("group-count-0","3");change("max-rounds","2");document.getElementById("add-group").click();change("group-type-1","t4g.nano");change("group-count-1","3");change("group-keep-0","4");change("group-keep-1","3");change("target-score","0")')
    browser.wait("state.imageStatus === 'ready'")
    assert browser.evaluate('document.getElementById("batch-total").textContent') == "6 台"
    assert browser.evaluate("buildOverrides().keep_top_k") == 7
    browser.screenshot(tmp_path / "quantities.png")
    browser.call("Emulation.setDeviceMetricsOverride", {"width": 390, "height": 844, "deviceScaleFactor": 1, "mobile": False})
    assert browser.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    browser.evaluate('document.getElementById("cfg-next").click()')
    browser.wait('!!document.getElementById("cf-start")')
    assert browser.evaluate('document.getElementById("content").innerText.includes("每轮 6 台")')
    browser.evaluate('document.getElementById("cf-start").click()')
    browser.wait("state.step === 5 && !!state.report")
    assert browser.evaluate("state.report.rounds_completed") == 2
    assert browser.evaluate("state.report.winners.length") == 7
    assert browser.evaluate("state.report.candidates.length") == 12

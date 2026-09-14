"""执行页面中的实际 JavaScript；模拟 DOM，无浏览器或网络请求。"""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_history_commands_unknown_cleanup_and_selection_overlay():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is needed to verify browser-side state handling")
    page = Path(__file__).parents[1] / "crossborder_selector/web/static/index.html"
    script = r"""
const fs = require("fs"), vm = require("vm"), assert = require("assert");
const html = fs.readFileSync(process.argv[1], "utf8");
const elements = new Map();
function element(key) {
  if (!elements.has(key)) elements.set(key, {
    innerHTML: "", textContent: "", hidden: false, disabled: false,
    addEventListener() {}, querySelectorAll() { return []; },
    classList: {toggle() {}}
  });
  return elements.get(key);
}
const context = vm.createContext({
  document: {querySelector: element}, window: {addEventListener() {}},
  console, setInterval: () => 0, clearInterval() {}, setTimeout: () => 0, clearTimeout() {},
  fetch: async () => ({ok: true, headers: {get: () => "application/json"}, json: async () => []})
});
vm.runInContext(html.match(/<script>([\s\S]*?)<\/script>/)[1], context);
vm.runInContext(`
  state.runId = "xb-history"; state.step = 4;
  showFailed("状态未知", null, []);
`, context);
assert(element("#fail-box").innerHTML.includes("尚未确认"));
assert(!/id="fail-clean"[^>]*disabled/.test(element("#fail-box").innerHTML));
vm.runInContext(`
  state.region = "ap-east-1";
  state.runDetail = {region: "us-west-2", selection: {
    selected: "i-a", protected: false, terminated: ["i-b"]
  }};
  state.selection = state.runDetail.selection;
  state.report = {winners: [
    {instance_id: "i-a", public_ip: "192.0.2.1", composite: 90},
    {instance_id: "i-b", public_ip: "192.0.2.2", composite: 80}
  ], candidates: [], prefixes: {}};
  mountDone();
`, context);
const done = element("#content").innerHTML;
assert(done.includes("--region us-west-2"));
assert(!done.includes("--region ap-east-1"));
assert(!done.includes('id="done-term"'));
vm.runInContext("mountResult()", context);
assert(element("#content").innerHTML.includes("已终止"));
vm.runInContext("state.runDetail.selection = null; mountResult()", context);
assert.strictEqual(vm.runInContext("state.selection", context), null);
vm.runInContext(`
  state.region = "ap-east-1";
  state.form = {instance_type: "t4g.nano", batch_size: 2, image_id: "", instance_overrides: [{}, {image_id:"ami-old"}]};
  state.imageChoice = "auto";
  state.instanceImageChoices = [, "ubuntu2404"];
  state.imageKey = currentImageKey();
  state.imageStatus = "ready";
  state.images = {images: [{key:"ubuntu2404", image_id:"ami-arm64"}]};
  syncImageSelections();
`, context);
assert.strictEqual(vm.runInContext("imageSelectionProblem()", context), "");
assert.strictEqual(vm.runInContext("state.form.instance_overrides[1].image_id", context), "ami-arm64");
vm.runInContext('state.region = "us-west-2"; state.images.images[0].image_id = "ami-stale"; syncImageSelections()', context);
assert(vm.runInContext("imageSelectionProblem()", context));
assert.strictEqual(vm.runInContext("state.form.instance_overrides[1].image_id", context), "ami-arm64");
vm.runInContext('state.imageKey = currentImageKey(); state.images.images = []', context);
assert(vm.runInContext("imageSelectionProblem()", context));
"""
    result = subprocess.run([node, "-e", script, str(page)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr

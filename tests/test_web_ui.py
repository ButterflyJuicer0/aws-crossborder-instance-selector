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


def test_failed_view_offers_way_back_home():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is needed to verify browser-side state handling")
    page = Path(__file__).parents[1] / "crossborder_selector/web/static/index.html"
    script = r"""
const fs = require("fs"), vm = require("vm"), assert = require("assert");
const html = fs.readFileSync(process.argv[1], "utf8");
const elements = new Map(), listeners = new Map();
function element(key) {
  if (!elements.has(key)) elements.set(key, {
    innerHTML: "", textContent: "", hidden: false, disabled: false,
    addEventListener(ev, fn) { listeners.set(key + ":" + ev, fn); }, querySelectorAll() { return []; },
    classList: {toggle() {}}
  });
  return elements.get(key);
}
const context = vm.createContext({
  document: {querySelector: element, getElementById: k => element("#" + k)}, window: {addEventListener() {}},
  console, setInterval: () => 0, clearInterval() {}, setTimeout: () => 0, clearTimeout() {},
  fetch: async () => ({ok: true, headers: {get: () => "application/json"}, json: async () => []}),
  location: {hash: ""}, history: {replaceState() {}}
});
vm.runInContext(html.match(/<script>([\s\S]*?)<\/script>/)[1], context);
vm.runInContext(`
  state.runId = "xb-failed"; state.step = 4; state.runState = "failed";
  showFailed("InsufficientInstanceCapacity: c6g.2xlarge in ap-east-2a", [], []);
`, context);
const box = element("#fail-box").innerHTML;
assert(box.includes('id="fail-home"'), "失败页需要返回首页按钮");
assert(listeners.has("#fail-home:click"), "返回首页按钮需要绑定点击");
let stepped = null;
vm.runInContext("goStep = (n) => { globalThis.__step = n; }", context);
listeners.get("#fail-home:click")();
assert.strictEqual(vm.runInContext("globalThis.__step", context), 2, "点击后应回到配置首页");
"""
    result = subprocess.run([node, "-e", script, str(page)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_config_stage_has_agent_panel_and_overrides_carry_agent_settings():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is needed to verify browser-side state handling")
    page = Path(__file__).parents[1] / "crossborder_selector/web/static/index.html"
    script = r"""
const fs = require("fs"), vm = require("vm"), assert = require("assert");
const html = fs.readFileSync(process.argv[1], "utf8");
const context = vm.createContext({
  document: {querySelector: () => ({innerHTML:"", textContent:"", hidden:false, addEventListener(){}, querySelectorAll(){return [];}, classList:{toggle(){}}}),
             getElementById: () => null},
  window: {addEventListener() {}}, console, setInterval: () => 0, clearInterval() {}, setTimeout: () => 0, clearTimeout() {},
  fetch: async () => ({ok: true, headers: {get: () => "application/json"}, json: async () => ({})}), location: {hash: ""}
});
vm.runInContext(html.match(/<script>([\s\S]*?)<\/script>/)[1], context);
vm.runInContext(`
  state.region = "ap-east-2";
  state.options = {defaults: {instance_type:"t3.nano", batch_size:2, max_rounds:1, keep_top_k:1, target_score:90, protect:false,
    reputation:{badlist_url:"https://x/y", require_badlist:true}, root_volume_type:"gp3", instance_overrides:[],
    backends:{agent:{enabled:true, transport:"http", tcp_ports:[443], probe_source_cidrs:[]}}},
    backends: [{name:"agent", enabled:true, needs_key:false, key_present:true, desc:"d"}], instance_types: [], errors: []};
  initForm();
`, context);
// 表单里有 agent 的端口与来源字段，默认取自配置
assert.strictEqual(vm.runInContext("JSON.stringify(state.form.agent.tcp_ports)", context), "[443]");
assert.strictEqual(vm.runInContext("JSON.stringify(state.form.agent.probe_source_cidrs)", context), "[]");
// 页面配置阶段渲染出 agent 面板
vm.runInContext(`state.imageChoice="auto"; state.instanceImageChoices=[]; state.form.instance_groups=[{instance_type:"t3.nano",count:2}];`, context);
const markup = vm.runInContext("agentPanelMarkup()", context);
assert(markup.includes('id="agent-panel"') && markup.includes('id="agent-tcp-ports"') && markup.includes('id="agent-probe-sources"'), markup.slice(0,300));
// 用户填写后进入 overrides，供 plan/run 使用
vm.runInContext(`state.form.agent.tcp_ports=[443,8443]; state.form.agent.probe_source_cidrs=["203.0.113.7/32"];`, context);
const ov = vm.runInContext("buildOverrides()", context);
assert.strictEqual(JSON.stringify(ov.backends.agent.tcp_ports), "[443,8443]");
assert.strictEqual(JSON.stringify(ov.backends.agent.probe_source_cidrs), '["203.0.113.7/32"]');
"""
    result = subprocess.run([node, "-e", script, str(page)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr

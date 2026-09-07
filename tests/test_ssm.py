from crossborder_selector.aws.ssm import SsmRunner


class FakeClock:
    def __init__(self): self.t = 0.0
    def __call__(self): return self.t
    def sleep(self, s): self.t += s


class FakeSsm:
    def __init__(self, online_after=1, invocation_statuses=None, output="CROSSBORDER_JSON:[]"):
        self.calls = 0
        self.online_after = online_after
        self.statuses = list(invocation_statuses or ["InProgress", "Success"])
        self.output = output
        self.sent = []

    def describe_instance_information(self, Filters):
        self.calls += 1
        ids = Filters[0]["Values"]
        status = "Online" if self.calls >= self.online_after else "ConnectionLost"
        return {"InstanceInformationList": [{"InstanceId": i, "PingStatus": status} for i in ids]}

    def send_command(self, **kw):
        self.sent.append(kw)
        return {"Command": {"CommandId": "cmd-1"}}

    def get_command_invocation(self, CommandId, InstanceId):
        st = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        return {"Status": st, "StandardOutputContent": self.output, "StandardErrorContent": ""}


def test_wait_online_returns_online_set():
    clk = FakeClock()
    r = SsmRunner(FakeSsm(online_after=2), sleeper=clk.sleep, clock=clk, poll_s=3)
    assert r.wait_online(["i-1", "i-2"], timeout_s=30) == {"i-1", "i-2"}


def test_wait_online_times_out_with_partial():
    clk = FakeClock()
    r = SsmRunner(FakeSsm(online_after=999), sleeper=clk.sleep, clock=clk, poll_s=5)
    assert r.wait_online(["i-1"], timeout_s=12) == set()
    assert clk.t >= 12


def test_run_script_polls_until_success():
    clk = FakeClock()
    fake = FakeSsm()
    r = SsmRunner(fake, sleeper=clk.sleep, clock=clk)
    status, out = r.run_script("i-1", "echo hi", timeout_s=60)
    assert status == "Success" and out.startswith("CROSSBORDER_JSON:")
    sent = fake.sent[0]
    assert sent["DocumentName"] == "AWS-RunShellScript" and sent["InstanceIds"] == ["i-1"]
    assert sent["Parameters"]["commands"] == ["echo hi"] and sent["TimeoutSeconds"] == 60
    assert sent["Parameters"]["executionTimeout"] == ["60"]  # 限制脚本运行时长，而非仅下发超时


def test_run_script_local_timeout():
    clk = FakeClock()
    r = SsmRunner(FakeSsm(invocation_statuses=["InProgress"]), sleeper=clk.sleep, clock=clk, poll_s=10)
    assert r.run_script("i-1", "sleep 999", timeout_s=25)[0] == "LocalTimeout"

"""通过 SSM 在候选机上执行脚本。不需要 SSH、key pair 或入站规则。"""
import time

from botocore.exceptions import ClientError

_TERMINAL = {"Success", "Failed", "TimedOut", "Cancelled", "DeliveryTimedOut", "Undeliverable", "Terminated"}


class SsmRunner:
    def __init__(self, client, sleeper=time.sleep, clock=time.time, poll_s=3):
        self.ssm, self._sleep, self._now, self.poll_s = client, sleeper, clock, poll_s

    def wait_online(self, instance_ids, timeout_s) -> set:
        ids, online, deadline = list(instance_ids), set(), self._now() + timeout_s
        while ids:
            r = self.ssm.describe_instance_information(
                Filters=[{"Key": "InstanceIds", "Values": ids}])
            for info in r.get("InstanceInformationList", []):
                if info.get("PingStatus") == "Online":
                    online.add(info["InstanceId"])
            if online >= set(ids) or self._now() >= deadline:
                break
            self._sleep(self.poll_s)
        return online

    def run_script(self, instance_id, script, timeout_s=120):
        cmd = self.ssm.send_command(
            InstanceIds=[instance_id], DocumentName="AWS-RunShellScript",
            # TimeoutSeconds 只约束下发（等待实例接单）；executionTimeout 才限制脚本实际运行时长
            Parameters={"commands": [script], "executionTimeout": [str(timeout_s)]},
            TimeoutSeconds=timeout_s)["Command"]["CommandId"]
        deadline = self._now() + timeout_s + 30
        while True:
            self._sleep(self.poll_s)
            try:
                inv = self.ssm.get_command_invocation(CommandId=cmd, InstanceId=instance_id)
            except ClientError as e:  # InvocationDoesNotExist：命令尚未下发到实例
                if e.response["Error"]["Code"] != "InvocationDoesNotExist":
                    raise
                inv = {"Status": "Pending"}
            if inv.get("Status") in _TERMINAL:
                return inv["Status"], (inv.get("StandardOutputContent", "") + inv.get("StandardErrorContent", ""))
            if self._now() >= deadline:
                return "LocalTimeout", ""

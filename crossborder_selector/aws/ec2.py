"""EC2 候选机生命周期。所有查询与删除都按 run-id 标签过滤。"""
import time
import uuid

from botocore.exceptions import ClientError

RUN_TAG = "crossborder-run-id"
ROUND_TAG = "crossborder-round"
WINNER_TAG = "crossborder-winner"
SCORE_TAG = "crossborder-score"
SELECTED_TAG = "crossborder-selected-at"
SOURCE_TAG = "crossborder-source-run"
_LIVE_STATES = ["pending", "running", "stopping", "stopped"]


class Ec2Manager:
    def __init__(self, client, sleeper=time.sleep):
        self.ec2 = client
        self._sleep = sleeper
        self.launch_settings = {}

    def launch(self, n, run_id, round_no, infra, instance_type) -> list:
        templates = infra.launch_templates
        if not templates:
            return self._launch_group(n, run_id, round_no, infra, instance_type, {"ImageId": infra.image_id})
        if all(template == templates[0] for template in templates):
            return self._launch_group(n, run_id, round_no, infra, instance_type, templates[0])
        if len(templates) != n:
            raise ValueError("launch template count must match batch_size")
        groups = []
        for template in templates:
            group = next((g for g in groups if g[0] == template), None)
            if group:
                group[1] += 1
            else:
                groups.append([template, 1])
        launched = []
        try:
            for template, count in groups:
                launched.extend(self._launch_group(count, run_id, round_no, infra, instance_type, template))
            return launched
        except BaseException:
            # Later groups may fail after earlier groups have already created billable resources.
            self.terminate(launched)
            raise

    def _launch_group(self, n, run_id, round_no, infra, instance_type, template):
        """启动 n 台；IAM profile 刚建好时 RunInstances 会报 InvalidParameterValue，重试最多 6 次。"""
        request = dict(template)
        instance_type = request.pop("InstanceType", instance_type)
        subnet_id = request.pop("SubnetId", infra.subnet_id)
        tags = [{"Key": RUN_TAG, "Value": run_id}, {"Key": ROUND_TAG, "Value": str(round_no)}]
        token = uuid.uuid4().hex
        last = None
        for _ in range(6):
            try:
                r = self.ec2.run_instances(
                    **request, InstanceType=instance_type, MinCount=n, MaxCount=n, ClientToken=token,
                    IamInstanceProfile={"Name": infra.instance_profile_name},
                    NetworkInterfaces=[{"DeviceIndex": 0, "SubnetId": subnet_id,
                                        "Groups": [infra.security_group_id],
                                        "AssociatePublicIpAddress": True}],
                    TagSpecifications=[{"ResourceType": "instance", "Tags": tags}],
                    InstanceInitiatedShutdownBehavior="terminate")
                ids = [i["InstanceId"] for i in r["Instances"]]
                self.launch_settings.update({iid: {**template, "InstanceType": instance_type, "SubnetId": subnet_id} for iid in ids})
                return ids
            except ClientError as e:
                last = e
                if e.response["Error"]["Code"] != "InvalidParameterValue":
                    raise
                self._sleep(5)
        raise last

    def wait_running(self, ids):
        if ids:
            self.ec2.get_waiter("instance_running").wait(InstanceIds=list(ids))

    def public_ips(self, ids) -> dict:
        out = {}
        if not ids:
            return out
        for res in self.ec2.describe_instances(InstanceIds=list(ids))["Reservations"]:
            for i in res["Instances"]:
                out[i["InstanceId"]] = i.get("PublicIpAddress", "")
        return out

    def terminate(self, ids):
        if ids:
            self.ec2.terminate_instances(InstanceIds=list(ids))

    def list_run_instances(self, run_id) -> list:
        return [i["InstanceId"] for i in self._run_resources(run_id)
                if not self._is_winner(i)]

    @staticmethod
    def _is_winner(instance):
        return any(t["Key"] == WINNER_TAG and t["Value"] == "true" for t in instance.get("Tags", []))

    def _run_resources(self, run_id):
        # Preserve ownership for both new resources and older winners with only a source-run tag.
        found = {}
        for tag in (RUN_TAG, SOURCE_TAG):
            for instance in self._instances([
                    {"Name": f"tag:{tag}", "Values": [run_id]},
                    {"Name": "instance-state-name", "Values": _LIVE_STATES}]):
                found[instance["InstanceId"]] = instance
        return list(found.values())

    def _instances(self, filters):
        args = {"Filters": filters}
        while True:
            response = self.ec2.describe_instances(**args)
            for reservation in response["Reservations"]:
                yield from reservation["Instances"]
            if not response.get("NextToken"):
                break
            args["NextToken"] = response["NextToken"]

    def list_run_winners(self, run_id):
        return [{"instance_id": i["InstanceId"], "public_ip": i.get("PublicIpAddress", "")}
                for i in self._run_resources(run_id) if self._is_winner(i)]

    def has_winners(self) -> bool:
        return any(self._instances([
            {"Name": f"tag:{WINNER_TAG}", "Values": ["true"]},
            {"Name": "instance-state-name", "Values": _LIVE_STATES}]))

    def mark_winner(self, instance_id, run_id, score, round_no, now_iso):
        # winner 长期运行，OS 内关机不应终止实例；改为 stop（stop/start 会换 IP，但比丢实例更安全）
        self.ec2.modify_instance_attribute(
            InstanceId=instance_id, InstanceInitiatedShutdownBehavior={"Value": "stop"})
        # Keep run ownership throughout the transition. Cleanup explicitly excludes committed winners.
        self.ec2.create_tags(Resources=[instance_id], Tags=[
            {"Key": WINNER_TAG, "Value": "true"}, {"Key": SCORE_TAG, "Value": f"{score:.1f}"},
            {"Key": ROUND_TAG, "Value": str(round_no)}, {"Key": SELECTED_TAG, "Value": now_iso},
            {"Key": SOURCE_TAG, "Value": run_id}])

    def protect(self, instance_id):
        self.ec2.modify_instance_attribute(InstanceId=instance_id, DisableApiTermination={"Value": True})
        self.ec2.modify_instance_attribute(InstanceId=instance_id, DisableApiStop={"Value": True})
        return {"termination": True, "stop": True}

    def unprotect(self, instance_id):
        # 与 protect 对称：终止候选前先解除保护，逐个属性 best-effort，忽略 ClientError
        for attr in ("DisableApiTermination", "DisableApiStop"):
            try:
                self.ec2.modify_instance_attribute(InstanceId=instance_id, **{attr: {"Value": False}})
            except ClientError:
                pass

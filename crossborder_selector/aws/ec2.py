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


_CAPACITY_CODES = {"InsufficientInstanceCapacity", "InsufficientCapacity"}


def instance_group_ids(instance: dict) -> list:
    """实例的安全组列表。用 NetworkInterfaces 启动时部分实现（含 moto）只在主网卡上报告组。"""
    groups = [g["GroupId"] for g in instance.get("SecurityGroups", [])]
    if not groups:
        for ni in instance.get("NetworkInterfaces", []):
            if ni.get("Attachment", {}).get("DeviceIndex", 0) == 0 or not groups:
                groups = [g["GroupId"] for g in ni.get("Groups", [])] or groups
    return groups


class Ec2Manager:
    def __init__(self, client, sleeper=time.sleep, log=None):
        self.ec2 = client
        self._sleep = sleeper
        self.log = log or (lambda m: None)
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
        """启动 n 台。某可用区容量不足（InsufficientInstanceCapacity）时依次换 infra.alternate_subnets 里
        的其他可用区子网重试；每个子网内部对 IAM profile 传播导致的 InvalidParameterValue 重试最多 6 次。"""
        request = dict(template)
        instance_type = request.pop("InstanceType", instance_type)
        primary = request.pop("SubnetId", infra.subnet_id)
        alternates = [s for s in getattr(infra, "alternate_subnets", {}).get(instance_type, []) if s != primary]
        tried, last = [], None
        for subnet_id in [primary, *alternates]:
            tried.append(subnet_id)
            try:
                return self._launch_in_subnet(n, run_id, round_no, infra, instance_type, template, request, subnet_id)
            except ClientError as e:
                last = e
                if e.response["Error"]["Code"] not in _CAPACITY_CODES:
                    raise
                nxt = alternates[len(tried) - 1] if len(tried) - 1 < len(alternates) else None
                self.log(f"{subnet_id} 容量不足（{instance_type}）"
                         + (f"，改用备选子网 {nxt} 重试" if nxt else "，已无备选子网"))
        msg = (f"{instance_type} 在已尝试的子网 {', '.join(tried)} 均容量不足；"
               "可换机型、换区域，或在 config.yaml 指定其他可用区的 subnet_id")
        raise ClientError({"Error": {"Code": last.response["Error"]["Code"], "Message": msg}}, "RunInstances") from last

    def _launch_in_subnet(self, n, run_id, round_no, infra, instance_type, template, request, subnet_id):
        tags = [{"Key": RUN_TAG, "Value": run_id}, {"Key": ROUND_TAG, "Value": str(round_no)}]
        token = uuid.uuid4().hex
        groups = [infra.security_group_id]
        probe_sg = getattr(infra, "probe_security_group_id", "")
        if probe_sg:
            groups.append(probe_sg)
        extra = {}
        if getattr(infra, "user_data", ""):
            extra["UserData"] = infra.user_data
        last = None
        for _ in range(6):
            try:
                r = self.ec2.run_instances(
                    **request, **extra, InstanceType=instance_type, MinCount=n, MaxCount=n, ClientToken=token,
                    IamInstanceProfile={"Name": infra.instance_profile_name},
                    NetworkInterfaces=[{"DeviceIndex": 0, "SubnetId": subnet_id,
                                        "Groups": groups,
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

    def detach_probe_group(self, instance_id: str, probe_group_id: str):
        """保留实例摘掉拨测组，只留下共享组；已不在列表时不动。"""
        inst = self.ec2.describe_instances(InstanceIds=[instance_id])["Reservations"][0]["Instances"][0]
        current = instance_group_ids(inst)
        remaining = [g for g in current if g != probe_group_id]
        if remaining != current and remaining:
            self.ec2.modify_instance_attribute(InstanceId=instance_id, Groups=remaining)
            self.log(f"{instance_id} 已摘除拨测安全组 {probe_group_id}")

    def delete_security_group(self, group_id: str, attempts: int = 6) -> bool:
        """删除安全组；实例终止有延迟时报 DependencyViolation，间隔重试。放弃时返回 False 而不抛，供清理兜底。"""
        for i in range(attempts):
            try:
                self.ec2.delete_security_group(GroupId=group_id)
                return True
            except ClientError as e:
                code = e.response["Error"]["Code"]
                if code == "InvalidGroup.NotFound":
                    return True
                if code != "DependencyViolation":
                    raise
                if i < attempts - 1:
                    self._sleep(10)
        self.log(f"拨测安全组 {group_id} 仍被引用，未删除；可稍后用 cleanup --run-id 再试")
        return False

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

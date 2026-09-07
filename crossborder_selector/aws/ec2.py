"""EC2 候选机生命周期。所有查询与删除都按 run-id 标签过滤。"""
import time

from botocore.exceptions import ClientError

RUN_TAG = "crossborder-run-id"
ROUND_TAG = "crossborder-round"
WINNER_TAG = "crossborder-winner"
SCORE_TAG = "crossborder-score"
SELECTED_TAG = "crossborder-selected-at"
_LIVE_STATES = ["pending", "running", "stopping", "stopped"]


class Ec2Manager:
    def __init__(self, client, sleeper=time.sleep):
        self.ec2 = client
        self._sleep = sleeper

    def launch(self, n, run_id, round_no, infra, instance_type) -> list:
        """启动 n 台；IAM profile 刚建好时 RunInstances 会报 InvalidParameterValue，重试最多 6 次。"""
        tags = [{"Key": RUN_TAG, "Value": run_id}, {"Key": ROUND_TAG, "Value": str(round_no)}]
        last = None
        for _ in range(6):
            try:
                r = self.ec2.run_instances(
                    ImageId=infra.image_id, InstanceType=instance_type, MinCount=1, MaxCount=n,
                    IamInstanceProfile={"Name": infra.instance_profile_name},
                    NetworkInterfaces=[{"DeviceIndex": 0, "SubnetId": infra.subnet_id,
                                        "Groups": [infra.security_group_id],
                                        "AssociatePublicIpAddress": True}],
                    TagSpecifications=[{"ResourceType": "instance", "Tags": tags}],
                    InstanceInitiatedShutdownBehavior="terminate")
                return [i["InstanceId"] for i in r["Instances"]]
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
        r = self.ec2.describe_instances(Filters=[
            {"Name": f"tag:{RUN_TAG}", "Values": [run_id]},
            {"Name": "instance-state-name", "Values": _LIVE_STATES}])
        return [i["InstanceId"] for res in r["Reservations"] for i in res["Instances"]]

    def mark_winner(self, instance_id, run_id, score, round_no, now_iso):
        # winner 长期运行，OS 内关机不应终止实例；改为 stop（stop/start 会换 IP，但比丢实例更安全）
        self.ec2.modify_instance_attribute(
            InstanceId=instance_id, InstanceInitiatedShutdownBehavior={"Value": "stop"})
        self.ec2.delete_tags(Resources=[instance_id], Tags=[{"Key": RUN_TAG}, {"Key": ROUND_TAG}])
        self.ec2.create_tags(Resources=[instance_id], Tags=[
            {"Key": WINNER_TAG, "Value": "true"}, {"Key": SCORE_TAG, "Value": f"{score:.1f}"},
            {"Key": ROUND_TAG, "Value": str(round_no)}, {"Key": SELECTED_TAG, "Value": now_iso},
            {"Key": "crossborder-source-run", "Value": run_id}])

    def protect(self, instance_id):
        self.ec2.modify_instance_attribute(InstanceId=instance_id, DisableApiTermination={"Value": True})
        try:
            self.ec2.modify_instance_attribute(InstanceId=instance_id, DisableApiStop={"Value": True})
        except ClientError:
            pass  # 少数机型/老 API 不支持 stop protection，不致命

    def has_winners(self) -> bool:
        r = self.ec2.describe_instances(Filters=[
            {"Name": f"tag:{WINNER_TAG}", "Values": ["true"]},
            {"Name": "instance-state-name", "Values": _LIVE_STATES}])
        return any(res["Instances"] for res in r["Reservations"])

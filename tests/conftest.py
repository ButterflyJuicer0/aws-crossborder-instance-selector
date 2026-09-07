"""让 moto 加载真实的 AWS 托管策略（默认不加载），使 attach_role_policy 能识别
arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore。必须在 moto 后端初始化前设置。"""
import os

os.environ.setdefault("MOTO_IAM_LOAD_MANAGED_POLICIES", "true")

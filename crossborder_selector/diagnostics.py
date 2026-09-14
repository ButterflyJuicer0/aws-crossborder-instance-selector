"""用于报告、运行日志和错误消息的密钥脱敏。"""
from urllib.parse import quote, quote_plus

SECRET_KEYS = {"api_key", "api_token", "abuseipdb_api_key"}


def secret_values(obj):
    if isinstance(obj, dict):
        return [str(v) for k, v in obj.items() if k in SECRET_KEYS and v] + [
            secret for v in obj.values() for secret in secret_values(v)]
    if isinstance(obj, list):
        return [secret for v in obj for secret in secret_values(v)]
    return []


def redact(obj, secrets=()):
    if isinstance(obj, dict):
        return {k: redact(v, secrets) for k, v in obj.items() if k not in SECRET_KEYS}
    if isinstance(obj, list):
        return [redact(v, secrets) for v in obj]
    if isinstance(obj, str):
        for secret in sorted(set(secrets), key=len, reverse=True):
            for value in (secret, quote(secret, safe=""), quote_plus(secret)):
                if value:
                    obj = obj.replace(value, "[redacted]")
    return obj

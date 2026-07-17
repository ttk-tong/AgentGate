"""API Key 生成、解析与哈希（plan/02 §1.1）。

密钥格式：ak_<prefix>_<secret>
- prefix：明文短前缀，落库便于识别/定位候选（非机密）。
- secret：随机明文，只在生成时返回一次；服务端只存 sha256(secret+盐) 的哈希。

纯函数、无 IO，可 `python -c` 直接自测。哈希用 hashlib（标准库），
不引 argon2 等重依赖——plan 允许 sha256+盐；生产可平滑替换为 argon2id。
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass

_PREFIX_TAG = "ak"
_PREFIX_LEN = 8   # 明文前缀长度（十六进制字符）
_SECRET_LEN = 32  # secret 字节数 → 64 hex 字符


@dataclass(frozen=True)
class GeneratedKey:
    """新生成的 API Key。full_key 只此一次可见，务必交给调用方保存。"""

    full_key: str   # ak_<prefix>_<secret>，明文，仅生成时返回
    prefix: str     # 落库，明文，用于定位候选
    key_hash: str   # 落库，sha256(secret+盐)


def generate_api_key(salt: str) -> GeneratedKey:
    """生成一把新 API Key。salt 为服务端机密（来自配置，不入库）。"""
    prefix = secrets.token_hex(_PREFIX_LEN // 2)
    secret = secrets.token_hex(_SECRET_LEN)
    full_key = f"{_PREFIX_TAG}_{prefix}_{secret}"
    return GeneratedKey(full_key=full_key, prefix=prefix, key_hash=hash_secret(secret, salt))


def parse_api_key(full_key: str) -> tuple[str, str] | None:
    """解析 ak_<prefix>_<secret> → (prefix, secret)。格式非法返回 None。"""
    if not full_key:
        return None
    parts = full_key.split("_")
    if len(parts) != 3 or parts[0] != _PREFIX_TAG:
        return None
    _, prefix, secret = parts
    if not prefix or not secret:
        return None
    return prefix, secret


def hash_secret(secret: str, salt: str) -> str:
    """sha256(salt + secret) 的十六进制摘要。salt 为服务端机密。"""
    h = hashlib.sha256()
    h.update(salt.encode("utf-8"))
    h.update(secret.encode("utf-8"))
    return h.hexdigest()


def verify_secret(secret: str, salt: str, expected_hash: str) -> bool:
    """常量时间比对，防时序侧信道。"""
    return hmac.compare_digest(hash_secret(secret, salt), expected_hash)


def strip_bearer(header_value: str | None) -> str | None:
    """从 Authorization 头取出 token，去掉可选的 'Bearer ' 前缀。"""
    if not header_value:
        return None
    value = header_value.strip()
    if value.lower().startswith("bearer "):
        return value[len("bearer ") :].strip()
    return value

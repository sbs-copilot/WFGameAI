# -*- coding: utf-8 -*-

# @Time    : 2025/11/11 10:35
# @Author  : Buker
# @File    : minio_helper
# @Desc    : MinIO 工具：从 settings.CFG._config 读取配置；支持权限设置、文件/文件夹上传下载、直链与预签名

from __future__ import annotations

import os
import json
import mimetypes
from pathlib import Path
from typing import Optional
from urllib.parse import quote
from concurrent.futures import ThreadPoolExecutor, as_completed

from django.conf import settings
from minio import Minio
from minio.error import S3Error


# 读取全局配置（config.ini）
def _get_minio_conf() -> dict:
    """
    从 settings.CFG._config 中解析 MinIO 配置。
    期望在 config.ini 中存在 [minio] 段；若不存在，回退到环境变量或默认值。

    支持的键（均为可选，未提供时使用回退值）：
      - endpoint: 例如 127.0.0.1:19000 或 https://minio.example.com（若包含 http/https 会用于 secure 推断）
      - secure: true/false（若 endpoint 以 https 开头，则强制 True）
      - access_key / secret_key（或 root_user / root_password 兼容字段名）
      - default_bucket: 默认桶名（可选）
      - server_url: 外部访问域名（可选，用于生成外部直链，默认使用 endpoint）
    """
    cfg = getattr(settings, 'CFG', None)
    ini = getattr(cfg, '_config', {}) if cfg else {}
    section = ini.get('minio', {}) if isinstance(ini, dict) else {}

    # 环境变量回退
    env_endpoint = os.getenv('MINIO_ENDPOINT')
    env_secure = os.getenv('MINIO_SECURE')
    env_access = os.getenv('MINIO_ACCESS_KEY') or os.getenv('MINIO_ROOT_USER')
    env_secret = os.getenv('MINIO_SECRET_KEY') or os.getenv('MINIO_ROOT_PASSWORD')
    env_bucket = os.getenv('MINIO_DEFAULT_BUCKET')
    env_server_url = os.getenv('MINIO_SERVER_URL')

    endpoint = (section.get('endpoint') if isinstance(section,
                                                      dict) else None) or env_endpoint or '172.28.133.200:19000'
    # 允许写 http(s)://host:port 或 host:port
    secure = False
    if str(endpoint).startswith('https://'):
        endpoint = endpoint.replace('https://', '')
        secure = True
    elif str(endpoint).startswith('http://'):
        endpoint = endpoint.replace('http://', '')
        secure = False
    else:
        # 未含协议则参考 secure 配置项
        if section and isinstance(section, dict) and 'secure' in section:
            secure = str(section.get('secure')).lower() == 'true'
        elif env_secure is not None:
            secure = str(env_secure).lower() == 'true'

    access_key = (
            (section.get('access_key') if isinstance(section, dict) else None)
            or (section.get('root_user') if isinstance(section, dict) else None)
            or env_access
            or 'root'
    )
    secret_key = (
            (section.get('secret_key') if isinstance(section, dict) else None)
            or (section.get('root_password') if isinstance(section, dict) else None)
            or env_secret
            or 'WfQa123456'
    )
    default_bucket = (section.get('default_bucket') if isinstance(section, dict) else None) or env_bucket or ''
    server_url = (section.get('server_url') if isinstance(section, dict) else None) or env_server_url

    return {
        'endpoint': endpoint,
        'secure': secure,
        'access_key': access_key,
        'secret_key': secret_key,
        'default_bucket': default_bucket,
        'server_url': server_url,  # 若为空则用 endpoint 生成 URL
    }


def get_client() -> Minio:
    conf = _get_minio_conf()
    return Minio(
        endpoint=conf['endpoint'],
        access_key=conf['access_key'],
        secret_key=conf['secret_key'],
        secure=conf['secure'],
    )


def _client() -> Optional[Minio]:
    """安全获取 Minio 客户端。失败时返回 None，不抛异常。"""
    try:
        return get_client()
    except Exception as e:
        print(f"[minio] 获取客户端失败: {e}")
        return None


class MinioService:
    """面向应用的 MinIO 封装类，统一对象路径语义。

    属性:
      client: Minio 原始客户端
      default_bucket: 默认桶名（可为空）
      server_url: 生成直链时的主机名（可为空回退 endpoint）

    方法分组:
      - bucket 管理: ensure_bucket, set_bucket_public_read, set_bucket_private
      - 文件: upload_file, download_file
      - 文件夹: upload_folder, download_folder
      - 查询与URL: list_objects, object_url, presigned_get_url

    所有涉及对象路径的参数均为完整逻辑路径，不使用 prefix 概念。
    """

    def __init__(self):
        conf = _get_minio_conf()
        self._conf = conf
        self.client = Minio(
            endpoint=conf['endpoint'],
            access_key=conf['access_key'],
            secret_key=conf['secret_key'],
            secure=conf['secure'],
        )
        self.default_bucket = conf.get('default_bucket') or ''
        self.server_url = conf.get('server_url') or conf.get('endpoint')

    # ---------- bucket ----------
    def ensure_bucket(self, bucket: str | None = None) -> Optional[str]:
        b = bucket or self.default_bucket
        if not b:
            print('[minio] ensure_bucket: 未指定 bucket 且未配置 default_bucket')
            return None
        try:
            if not self.client.bucket_exists(b):
                self.client.make_bucket(b)
            return b
        except Exception as e:
            print(f"[minio] ensure_bucket 失败: {e}")
            return None

    def set_bucket_public_read(self, bucket: str | None = None) -> bool:
        b = self.ensure_bucket(bucket)
        if not b:
            return False
        policy = {
            "Version": "2012-10-17",
            "Statement": [{
                "Effect": "Allow",
                "Principal": {"AWS": ["*"]},
                "Action": ["s3:GetObject"],
                "Resource": [f"arn:aws:s3:::{b}/*"],
            }],
        }
        try:
            self.client.set_bucket_policy(b, json.dumps(policy))
            return True
        except Exception as e:
            print(f"[minio] set_bucket_public_read 失败: {e}")
            return False

    def set_bucket_private(self, bucket: str | None = None) -> bool:
        b = self.ensure_bucket(bucket)
        if not b:
            return False
        policy = {"Version": "2012-10-17", "Statement": []}
        try:
            self.client.set_bucket_policy(b, json.dumps(policy))
            return True
        except Exception as e:
            print(f"[minio] set_bucket_private 失败: {e}")
            return False

    # ---------- URL ----------
    def object_url(self, bucket: str, key: str) -> str:
        scheme = 'https' if self._conf['secure'] else 'http'
        host = self.server_url.rstrip('/')
        return f"{scheme}://{host}/{bucket}/{quote(key, safe='/~')}"

    def presigned_get_url(self, bucket: str, key: str, expires_seconds: int = 3600) -> str:
        return self.client.presigned_get_object(bucket, key, expires=expires_seconds)

    # ---------- 文件 ----------
    def upload_file(self, bucket: str | None, object_name: str, file_path: str,
                    content_type: Optional[str] = None) -> Optional[str]:
        b = self.ensure_bucket(bucket)
        if not b:
            return None
        p = Path(file_path)
        if not p.is_file():
            print(f"[minio] upload_file: 文件不存在 {file_path}")
            return None
        ct = content_type or _guess_mime(p)
        try:
            self.client.fput_object(b, object_name, str(p), content_type=ct)
            return self.object_url(b, object_name)
        except Exception as e:
            print(f"[minio] upload_file 失败: {e}")
            return None

    def download_file(self, bucket: str | None, object_name: str, dest_path: str) -> Optional[str]:
        b = self.ensure_bucket(bucket)
        if not b:
            return None
        dest = Path(dest_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.client.fget_object(b, object_name, str(dest))
            return str(dest)
        except Exception as e:
            print(f"[minio] download_file 失败: {e}")
            return None

    # ---------- 文件夹 ----------
    def upload_folder(self, bucket: str | None, local_dir: str, object_root_path: str, include_hidden: bool = False) -> list[str]:
        b = self.ensure_bucket(bucket)
        if not b:
            return []
        base = Path(local_dir)
        if not base.is_dir():
            print(f"[minio] upload_folder: 不是有效目录 {local_dir}")
            return []
        keys: list[str] = []
        root = (object_root_path or '').lstrip('/')
        for path in base.rglob('*'):
            if path.is_dir():
                continue
            if not include_hidden and any(part.startswith('.') for part in path.parts):
                continue
            rel = path.relative_to(base).as_posix()
            key = f'{root}/{rel}' if root else rel
            ct = _guess_mime(path)
            try:
                self.client.fput_object(b, key, str(path), content_type=ct)
                keys.append(key)
            except Exception as e:
                print(f"[minio] upload_folder 单文件失败 {path}: {e}")
        return keys

    def download_folder(self, bucket: str | None, object_root_path: str, local_dir: str) -> list[str]:
        b = self.ensure_bucket(bucket)
        if not b:
            return []
        dest_root = Path(local_dir)
        dest_root.mkdir(parents=True, exist_ok=True)
        saved: list[str] = []
        prefix = (object_root_path or '').lstrip('/')
        try:
            for obj in self.client.list_objects(b, prefix=prefix, recursive=True):
                if obj.object_name.endswith('/'):
                    continue
                rel = obj.object_name[len(prefix):].lstrip('/') if prefix else obj.object_name
                local_path = dest_root / rel
                local_path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    self.client.fget_object(b, obj.object_name, str(local_path))
                    saved.append(str(local_path))
                except Exception as e:
                    print(f"[minio] 下载失败 {obj.object_name}: {e}")
        except Exception as e:
            print(f"[minio] 列举对象失败: {e}")
        return saved

    # ---------- 查询 ----------
    def list_objects(self, bucket: str | None, object_root_path: str = '') -> list[str]:
        b = self.ensure_bucket(bucket)
        if not b:
            return []
        prefix = (object_root_path or '').lstrip('/')
        try:
            return [o.object_name for o in self.client.list_objects(b, prefix=prefix, recursive=True)]
        except Exception as e:
            print(f"[minio] list_objects 失败: {e}")
            return []

    # ---------- 便捷(默认桶) ----------
    def upload_file_default(self, object_name: str, file_path: str, content_type: Optional[str] = None) -> str:
        return self.upload_file(None, object_name, file_path, content_type)

    def upload_folder_default(self, local_dir: str, object_root_path: str, include_hidden: bool = False) -> list[str]:
        return self.upload_folder(None, local_dir, object_root_path, include_hidden)

    def download_folder_default(self, object_root_path: str, local_dir: str) -> list[str]:
        return self.download_folder(None, object_root_path, local_dir)

    def set_public_default(self) -> None:
        self.set_bucket_public_read(None)

    def set_private_default(self) -> None:
        self.set_bucket_private(None)


def ensure_bucket(bucket: str) -> bool:
    client = _client()
    if not client:
        return False
    try:
        if not client.bucket_exists(bucket):
            client.make_bucket(bucket)
        return True
    except Exception as e:
        print(f"[minio] ensure_bucket 失败: {e}")
        return False


def set_bucket_public_read(bucket: str) -> bool:
    """将桶设置为匿名可读（允许任何人 GET 对象）。"""
    if not ensure_bucket(bucket):
        return False
    policy = {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"AWS": ["*"]},
            "Action": ["s3:GetObject"],
            "Resource": [f"arn:aws:s3:::{bucket}/*"],
        }],
    }
    client = _client()
    if not client:
        return False
    try:
        client.set_bucket_policy(bucket, json.dumps(policy))
        return True
    except Exception as e:
        print(f"[minio] set_bucket_public_read 失败: {e}")
        return False


def set_bucket_private(bucket: str) -> bool:
    """将桶恢复为私有（移除匿名访问）。"""
    if not ensure_bucket(bucket):
        return False
    client = _client()
    if not client:
        return False
    try:
        policy = {"Version": "2012-10-17", "Statement": []}
        client.set_bucket_policy(bucket, json.dumps(policy))
        return True
    except Exception as e:
        print(f"[minio] set_bucket_private 失败: {e}")
        return False


def _guess_mime(path: Path) -> str:
    ctype, _ = mimetypes.guess_type(str(path))
    return ctype or 'application/octet-stream'


def object_url(bucket: str, key: str) -> str:
    """生成对象直链 URL：需桶已设置为匿名可读。"""
    conf = _get_minio_conf()
    scheme = 'https' if conf['secure'] else 'http'
    host = conf['server_url'].rstrip('/') if conf['server_url'] else conf['endpoint']
    return f"{scheme}://{host}/{bucket}/{quote(key, safe='/~')}"


def presigned_get_url(bucket: str, key: str, expires_seconds: int = 3600) -> Optional[str]:
    client = _client()
    if not client:
        return None
    try:
        return client.presigned_get_object(bucket, key, expires=expires_seconds)
    except Exception as e:
        print(f"[minio] 生成预签名URL失败: {e}")
        return None


def upload_file(bucket: str, object_name: str, file_path: str, content_type: Optional[str] = None) -> Optional[str]:
    """上传单个文件，返回直链（若桶已公开）或可用于拼接的路径。"""
    if not ensure_bucket(bucket):
        return None
    p = Path(file_path)
    if not p.is_file():
        print(f"[minio] upload_file: 文件不存在 {file_path}")
        return None
    ct = content_type or _guess_mime(p)
    client = _client()
    if not client:
        return None
    try:
        client.fput_object(bucket, object_name, str(p), content_type=ct)
        return object_url(bucket, object_name)
    except Exception as e:
        print(f"[minio] upload_file 失败: {e}")
        return None


def upload_file_deduplicated(bucket: str, file_path: str, object_path: str = '',
                             content_type: Optional[str] = None) -> Optional[str]:
    """
    去重上传（独立函数版）：使用文件内容的哈希值作为对象名。
    """
    if not ensure_bucket(bucket):
        return None
    p = Path(file_path)
    if not p.is_file():
        print(f"[minio] upload_file_deduplicated: 文件不存在 {file_path}")
        return None
    object_path = object_path.lstrip('/')

    client = _client()
    if not client:
        return None

    try:
        client.stat_object(bucket, object_path)
        return object_url(bucket, object_path)
    except S3Error as e:
        if e.code == 'NoSuchKey':
            return upload_file(bucket, object_path, str(p), content_type)
        else:
            print(f"[minio] check existence failed: {e}")
            return None


def batch_upload_file_deduplicated(bucket: str, items: list[tuple], max_workers: int = 20) -> dict[str, str]:
    """
    批量去重上传，使用线程池并发处理以最小化时间复杂度。
    无需重复初始化客户端和检查 bucket。

    :param bucket: 目标桶名
    :param items: 待上传项列表，每项为一个元组:
                  (local_file_path, object_path) 或 (local_file_path, object_path, content_type)
    :param max_workers: 并发线程数，默认 20
    :return: 成功项字典 {object_path: url}
    """
    if not ensure_bucket(bucket):
        return {}

    client = _client()
    if not client:
        return {}

    results = {}

    def _process_one(item: tuple) -> tuple[str, Optional[str]]:
        # 解析参数
        if len(item) == 3:
            f_path, o_path, c_type = item
        elif len(item) == 2:
            f_path, o_path = item
            c_type = None
        else:
            return "", None

        o_path = o_path.lstrip('/')
        p = Path(f_path)

        if not p.is_file():
            # print(f"[minio] batch: 文件不存在 {f_path}")
            return o_path, None

        # 1. 检查是否存在 (stat_object 是轻量级元数据请求)
        try:
            client.stat_object(bucket, o_path)
            # 已存在，直接返回 URL
            return o_path, object_url(bucket, o_path)
        except S3Error as e:
            if e.code != 'NoSuchKey':
                print(f"[minio] batch check failed {o_path}: {e}")
                return o_path, None
            # NoSuchKey -> 继续执行上传
        except Exception as e:
            print(f"[minio] batch check error {o_path}: {e}")
            return o_path, None

        # 2. 上传文件
        ct = c_type or _guess_mime(p)
        try:
            client.fput_object(bucket, o_path, str(p), content_type=ct)
            return o_path, object_url(bucket, o_path)
        except Exception as e:
            print(f"[minio] batch upload failed {o_path}: {e}")
            return o_path, None

    # 使用线程池并发处理
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        # 提交任务
        future_to_path = {executor.submit(_process_one, item): item for item in items}

        for future in as_completed(future_to_path):
            try:
                o_path, url = future.result()
                if o_path and url:
                    results[o_path] = url
            except Exception as e:
                print(f"[minio] batch task exception: {e}")

    return results


def download_file(bucket: str, object_name: str, dest_path: str) -> Optional[str]:
    """下载单个文件到本地，返回保存路径。"""
    dest = Path(dest_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    client = _client()
    if not client:
        return None
    try:
        client.fget_object(bucket, object_name, str(dest))
        return str(dest)
    except Exception as e:
        print(f"[minio] download_file 失败: {e}")
        return None


def upload_folder(bucket: str, local_dir: str, object_root_path: str,
                  include_hidden: bool = False) -> list[str]:
    """
    递归上传文件夹内容到桶内指定的根路径 object_root_path 下。
    参数语义：
      - local_dir: 本地源目录 (必须存在且是目录)
      - object_root_path: 目标桶内根路径，形如 'reports/2025/11' 或 '/reports/2025/11'；不需要也不应该使用 prefix 概念。
      - include_hidden: 是否包含以 . 开头的隐藏文件/目录。

    路径规则：
      1. object_root_path 会被标准化去掉前导 '/'
      2. 最终对象键 = object_root_path + '/' + 相对路径
      3. 若 object_root_path 为空字符串，则直接使用相对路径作为对象键

    返回：上传的对象键列表。
    """
    base = Path(local_dir)
    if not base.is_dir():
        raise NotADirectoryError(f"不是有效目录: {local_dir}")
    if not ensure_bucket(bucket):
        return []
    keys: list[str] = []
    root = (object_root_path or '').lstrip('/')
    for path in base.rglob('*'):
        if path.is_dir():
            continue
        if not include_hidden and any(part.startswith('.') for part in path.parts):
            continue
        rel = path.relative_to(base).as_posix()
        key = f"{root}/{rel}" if root else rel
        ct = _guess_mime(path)
        client = _client()
        if not client:
            return keys
        try:
            client.fput_object(bucket, key, str(path), content_type=ct)
            keys.append(key)
        except Exception as e:
            print(f"[minio] upload_folder 单文件失败 {path}: {e}")
    return keys


def download_folder(bucket: str, object_root_path: str, local_dir: str) -> list[str]:
    """
    下载桶内指定 object_root_path (视为前缀) 下的所有对象到本地目录，保持相对层级结构。
    参数：
      - object_root_path: 形如 'reports/2025/11' 或 '/reports/2025/11'
      - local_dir: 本地保存根目录
    返回：本地保存的文件绝对路径列表。
    """
    dest_root = Path(local_dir)
    dest_root.mkdir(parents=True, exist_ok=True)
    saved: list[str] = []
    prefix = (object_root_path or '').lstrip('/')
    client = _client()
    if not client:
        return saved
    for obj in client.list_objects(bucket, prefix=prefix, recursive=True):
        if obj.object_name.endswith('/'):
            continue
        rel = obj.object_name[len(prefix):].lstrip('/') if prefix else obj.object_name
        local_path = dest_root / rel
        local_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            client.fget_object(bucket, obj.object_name, str(local_path))
        except Exception as e:
            print(f"[minio] 下载失败 {obj.object_name}: {e}")
        saved.append(str(local_path))
    return saved


def list_objects(bucket: str, object_root_path: str = '') -> list[str]:
    prefix = (object_root_path or '').lstrip('/')
    client = _client()
    if not client:
        return []
    try:
        return [o.object_name for o in client.list_objects(bucket, prefix=prefix, recursive=True)]
    except Exception as e:
        print(f"[minio] list_objects 失败: {e}")
        return []


# 便捷入口：使用默认桶
def upload_file_default(object_name: str, file_path: str, content_type: Optional[str] = None) -> Optional[str]:
    conf = _get_minio_conf()
    bucket = conf.get('default_bucket') or ''
    if not bucket:
        print("[minio] 未配置 default_bucket")
        return None
    return upload_file(bucket, object_name, file_path, content_type)


def upload_folder_default(local_dir: str, object_root_path: str, include_hidden: bool = False) -> list[str]:
    conf = _get_minio_conf()
    bucket = conf.get('default_bucket') or ''
    if not bucket:
        print("[minio] 未配置 default_bucket")
        return []
    return upload_folder(bucket, local_dir, object_root_path, include_hidden)


def download_folder_default(object_root_path: str, local_dir: str) -> list[str]:
    conf = _get_minio_conf()
    bucket = conf.get('default_bucket') or ''
    if not bucket:
        print("[minio] 未配置 default_bucket")
        return []
    return download_folder(bucket, object_root_path, local_dir)


def set_public_default() -> bool:
    conf = _get_minio_conf()
    bucket = conf.get('default_bucket') or ''
    if not bucket:
        print("[minio] 未配置 default_bucket")
        return False
    return set_bucket_public_read(bucket)


def set_private_default() -> bool:
    conf = _get_minio_conf()
    bucket = conf.get('default_bucket') or ''
    if not bucket:
        print("[minio] 未配置 default_bucket")
        return False
    return set_bucket_private(bucket)


if __name__ == "__main__":
    bucket_name = "wfgame-ai"
    # set_bucket_private(bucket_name)
    set_bucket_public_read(bucket_name)
    # url = upload_file(bucket_name, "test-folder/hello.txt", "/Users/hbuker/Desktop/WFGameAI/.gitmodules", content_type="text/plain")
    # print(f"Uploaded file URL: {url}")

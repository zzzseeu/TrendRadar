# coding=utf-8
"""
远程存储后端（S3 兼容协议）

支持 Cloudflare R2、阿里云 OSS、腾讯云 COS、AWS S3、MinIO 等
使用 S3 兼容 API (boto3) 访问对象存储
数据流程：下载当天 SQLite → 合并新数据 → 上传回远程
"""

import pytz
import re
import shutil
import sys
import tempfile
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

try:
    import boto3
    from botocore.config import Config as BotoConfig
    from botocore.exceptions import ClientError
    HAS_BOTO3 = True
except ImportError:
    HAS_BOTO3 = False
    boto3 = None
    BotoConfig = None
    ClientError = Exception

from trendradar.storage.base import StorageBackend, NewsData, RSSItem, RSSData
from trendradar.storage.sqlite_mixin import SQLiteStorageMixin
from trendradar.utils.time import (
    DEFAULT_TIMEZONE,
    get_configured_time,
    format_date_folder,
    format_time_filename,
)


class RemoteStorageBackend(SQLiteStorageMixin, StorageBackend):
    """
    远程云存储后端（S3 兼容协议）

    特点：
    - 使用 S3 兼容 API 访问远程存储
    - 支持 Cloudflare R2、阿里云 OSS、腾讯云 COS、AWS S3、MinIO 等
    - 下载 SQLite 到临时目录进行操作
    - 支持数据合并和上传
    - 支持从远程拉取历史数据到本地
    - 运行结束后自动清理临时文件
    """

    def __init__(
        self,
        bucket_name: str,
        access_key_id: str,
        secret_access_key: str,
        endpoint_url: str,
        region: str = "",
        enable_txt: bool = False,  # 远程模式默认不生成 TXT
        enable_html: bool = True,
        temp_dir: Optional[str] = None,
        timezone: str = DEFAULT_TIMEZONE,
    ):
        """
        初始化远程存储后端

        Args:
            bucket_name: 存储桶名称
            access_key_id: 访问密钥 ID
            secret_access_key: 访问密钥
            endpoint_url: 服务端点 URL
            region: 区域（可选，部分服务商需要）
            enable_txt: 是否启用 TXT 快照（默认关闭）
            enable_html: 是否启用 HTML 报告
            temp_dir: 临时目录路径（默认使用系统临时目录）
            timezone: 时区配置
        """
        if not HAS_BOTO3:
            raise ImportError("远程存储后端需要安装 boto3: pip install boto3")

        self.bucket_name = bucket_name
        self.endpoint_url = endpoint_url
        self.region = region
        self.enable_txt = enable_txt
        self.enable_html = enable_html
        self.timezone = timezone

        # 创建临时目录
        self.temp_dir = Path(temp_dir) if temp_dir else Path(tempfile.mkdtemp(prefix="trendradar_"))
        self.temp_dir.mkdir(parents=True, exist_ok=True)

        # 初始化 S3 客户端
        # 使用 virtual-hosted style addressing（主流）
        # 根据服务商选择签名版本：
        # - 腾讯云 COS 和 阿里云 OSS 使用 SigV2 以避免 chunked encoding 问题
        # - 其他服务商（AWS S3、Cloudflare R2、MinIO 等）默认使用 SigV4
        use_sigv2 = "myqcloud.com" in endpoint_url.lower() or "aliyuncs.com" in endpoint_url.lower()
        signature_version = 's3' if use_sigv2 else 's3v4'

        s3_config = BotoConfig(
            s3={"addressing_style": "virtual"},
            signature_version=signature_version,
        )

        client_kwargs = {
            "endpoint_url": endpoint_url,
            "aws_access_key_id": access_key_id,
            "aws_secret_access_key": secret_access_key,
            "config": s3_config,
        }
        if region:
            client_kwargs["region_name"] = region

        self.s3_client = boto3.client("s3", **client_kwargs)

        # 跟踪下载的文件（用于清理）
        self._downloaded_files: List[Path] = []
        self._db_connections: Dict[str, sqlite3.Connection] = {}

        # 批量模式：延迟上传，避免频繁上传同一文件
        self._batch_mode = False
        self._batch_dirty: set = set()  # 待上传的 (date, db_type) 集合
        self._batch_snapshots: Dict[tuple, bytes] = {}
        self._batch_failed = False
        self._remote_provenance: Dict[str, Optional[tuple]] = {}
        self._strict_local_authoritative: set[str] = set()
        self._first_seen_needs_upload = False

        print(f"[远程存储] 初始化完成，存储桶: {bucket_name}，签名版本: {signature_version}")

    @property
    def backend_name(self) -> str:
        return "remote"

    @property
    def supports_txt(self) -> bool:
        return self.enable_txt

    # ========================================
    # SQLiteStorageMixin 抽象方法实现
    # ========================================

    def _get_configured_time(self) -> datetime:
        """获取配置时区的当前时间"""
        return get_configured_time(self.timezone)

    def _format_date_folder(self, date: Optional[str] = None) -> str:
        """格式化日期文件夹名 (ISO 格式: YYYY-MM-DD)"""
        if date is None:
            return self._get_configured_time().strftime("%Y-%m-%d")
        return format_date_folder(date, self.timezone)

    def _format_time_filename(self) -> str:
        """格式化时间文件名 (格式: HH-MM)"""
        return format_time_filename(self.timezone)

    def _get_remote_db_key(self, date: Optional[str] = None, db_type: str = "news") -> str:
        """
        获取远程存储中 SQLite 文件的对象键

        Args:
            date: 日期字符串
            db_type: 数据库类型 ("news" 或 "rss")

        Returns:
            远程对象键，如 "news/2025-12-28.db" 或 "rss/2025-12-28.db"
        """
        date_folder = self._format_date_folder(date)
        return f"{db_type}/{date_folder}.db"

    def _get_local_db_path(self, date: Optional[str] = None, db_type: str = "news") -> Path:
        """
        获取本地临时 SQLite 文件路径

        Args:
            date: 日期字符串
            db_type: 数据库类型 ("news" 或 "rss")

        Returns:
            本地临时文件路径
        """
        date_folder = self._format_date_folder(date)
        db_dir = self.temp_dir / db_type
        db_dir.mkdir(parents=True, exist_ok=True)
        return db_dir / f"{date_folder}.db"

    def _check_object_exists(self, r2_key: str, strict: bool = False) -> bool:
        """
        检查远程存储中对象是否存在

        Args:
            r2_key: 远程对象键

        Returns:
            是否存在
        """
        try:
            self.s3_client.head_object(Bucket=self.bucket_name, Key=r2_key)
            return True
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            # S3 兼容存储可能返回 404, NoSuchKey, 或其他变体
            if error_code in ("404", "NoSuchKey", "Not Found"):
                return False
            # 普通存储路径保持既有宽松语义；严格读取路径必须上抛错误。
            print(f"[远程存储] 检查对象存在性失败 ({r2_key}): {e}")
            if strict:
                raise
            return False
        except Exception as e:
            print(f"[远程存储] 检查对象存在性异常 ({r2_key}): {e}")
            if strict:
                raise
            return False

    def _download_sqlite(
        self,
        date: Optional[str] = None,
        db_type: str = "news",
        strict_exists: bool = False,
    ) -> Optional[Path]:
        """
        从远程存储下载当天的 SQLite 文件到本地临时目录

        使用 get_object + iter_chunks 替代 download_file，
        以正确处理腾讯云 COS 的 chunked transfer encoding。

        Args:
            date: 日期字符串
            db_type: 数据库类型 ("news" 或 "rss")

        Returns:
            本地文件路径，如果不存在返回 None
        """
        r2_key = self._get_remote_db_key(date, db_type)
        local_path = self._get_local_db_path(date, db_type)

        # 确保目录存在
        local_path.parent.mkdir(parents=True, exist_ok=True)

        # 先检查文件是否存在
        if strict_exists:
            object_exists = self._check_object_exists(r2_key, strict=True)
        else:
            object_exists = self._check_object_exists(r2_key)
        if not object_exists:
            print(f"[远程存储] 文件不存在，将创建新数据库: {r2_key}")
            return None

        try:
            # 使用 get_object + iter_chunks 替代 download_file
            # iter_chunks 会自动处理 chunked transfer encoding
            response = self.s3_client.get_object(Bucket=self.bucket_name, Key=r2_key)
            with open(local_path, 'wb') as f:
                for chunk in response['Body'].iter_chunks(chunk_size=1024*1024):
                    f.write(chunk)
            self._downloaded_files.append(local_path)
            if hasattr(self, "_strict_local_authoritative"):
                self._strict_local_authoritative.discard(r2_key)
            print(f"[远程存储] 已下载: {r2_key} -> {local_path}")
            return local_path
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            # S3 兼容存储可能返回不同的错误码
            if error_code in ("404", "NoSuchKey", "Not Found"):
                print(f"[远程存储] 文件不存在，将创建新数据库: {r2_key}")
                return None
            else:
                print(f"[远程存储] 下载失败 (错误码: {error_code}): {e}")
                raise
        except Exception as e:
            print(f"[远程存储] 下载异常: {e}")
            raise

    @staticmethod
    def _provenance_from_head(response: Dict) -> tuple:
        modified = response.get("LastModified")
        if hasattr(modified, "isoformat"):
            modified = modified.isoformat()
        return (
            response.get("VersionId") or "",
            response.get("ETag") or "",
            str(modified or ""),
        )

    def _head_provenance_strict(self, remote_key: str) -> Optional[tuple]:
        try:
            response = self.s3_client.head_object(
                Bucket=self.bucket_name, Key=remote_key
            )
            return self._provenance_from_head(response)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("404", "NoSuchKey", "Not Found"):
                return None
            raise

    def _close_cached_connection(self, local_path: Path) -> None:
        db_path = str(local_path)
        connection = self._db_connections.pop(db_path, None)
        if connection is not None:
            connection.close()

    def _download_object_atomic_strict(
        self, remote_key: str, local_path: Path
    ) -> None:
        response = self.s3_client.get_object(
            Bucket=self.bucket_name, Key=remote_key
        )
        local_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = local_path.with_name(f".{local_path.name}.refresh")
        try:
            with open(temporary_path, "wb") as target:
                for chunk in response["Body"].iter_chunks(
                    chunk_size=1024 * 1024
                ):
                    target.write(chunk)
            temporary_path.replace(local_path)
        finally:
            if temporary_path.exists():
                temporary_path.unlink()
        if local_path not in self._downloaded_files:
            self._downloaded_files.append(local_path)

    def _refresh_remote_sqlite_strict(
        self, remote_key: str, local_path: Path
    ) -> bool:
        """按远端对象版本刷新严格读取缓存。"""
        if not hasattr(self, "_remote_provenance"):
            self._remote_provenance = {}
        if not hasattr(self, "_strict_local_authoritative"):
            self._strict_local_authoritative = set()
        provenance = self._head_provenance_strict(remote_key)
        cached = self._remote_provenance.get(remote_key)
        has_cached_provenance = remote_key in self._remote_provenance
        if (
            has_cached_provenance
            and provenance != cached
            and remote_key in self._strict_local_authoritative
        ):
            raise RuntimeError(
                f"远程对象版本已变化，拒绝覆盖本地修改: {remote_key}"
            )
        if remote_key in self._strict_local_authoritative:
            # 相同 baseline 下，strict read 必须继续消费本地 authoritative
            # 状态；只有 CAS 成功或显式回滚才能清除 dirty。
            return local_path.exists()
        if provenance is None:
            # 只有明确由当前进程新建/修改的本地数据库可在远端 404 时保留；
            # 普通读取留下的旧缓存必须失效。
            if (
                local_path.exists()
                and remote_key not in self._strict_local_authoritative
            ):
                self._close_cached_connection(local_path)
                local_path.unlink()
            self._remote_provenance[remote_key] = None
            return local_path.exists()
        if provenance != cached or not local_path.exists():
            self._close_cached_connection(local_path)
            self._download_object_atomic_strict(remote_key, local_path)
            confirmed = self._head_provenance_strict(remote_key)
            if confirmed != provenance:
                if local_path.exists():
                    local_path.unlink()
                raise RuntimeError(
                    f"远程对象下载期间版本已变化: {remote_key}"
                )
            provenance = confirmed
        self._strict_local_authoritative.discard(remote_key)
        self._remote_provenance[remote_key] = provenance
        return True

    def _mark_strict_local_dirty(self, remote_key: str) -> None:
        if not hasattr(self, "_strict_local_authoritative"):
            self._strict_local_authoritative = set()
        self._strict_local_authoritative.add(remote_key)

    def _restore_local_sqlite_snapshot(
        self, local_path: Path, remote_key: str, content: bytes
    ) -> None:
        """CAS 失败时恢复 mutation 前本地镜像并清除 dirty。"""
        self._close_cached_connection(local_path)
        for suffix in ("-wal", "-shm"):
            auxiliary = Path(f"{local_path}{suffix}")
            if auxiliary.exists():
                auxiliary.unlink()
        temporary_path = local_path.with_name(
            f".{local_path.name}.rollback"
        )
        temporary_path.write_bytes(content)
        temporary_path.replace(local_path)
        self._strict_local_authoritative.discard(remote_key)

    def _begin_news_mutation(self, date: Optional[str]) -> Dict[str, object]:
        """绑定共享 news baseline，并保存本次 mutation 前镜像。"""
        date_str = self._format_date_folder(date)
        token = (date_str, "news")
        local_path = self._get_local_db_path(date_str, "news")
        remote_key = self._get_remote_db_key(date_str, "news")
        conn = self._get_ai_connection(date_str, strict=True)
        conn.commit()
        before = local_path.read_bytes()
        had_pending = token in self._batch_dirty
        if self._batch_mode:
            if not hasattr(self, "_batch_snapshots"):
                self._batch_snapshots = {}
            self._batch_snapshots.setdefault(token, before)
        # 在任何底层 helper 再次请求连接前标记 authoritative，防止
        # snapshot 与实际 mutation 之间被远端 refresh 覆盖。
        self._mark_strict_local_dirty(remote_key)
        return {
            "date": date_str,
            "token": token,
            "path": local_path,
            "remote_key": remote_key,
            "before": before,
            "had_pending": had_pending,
            "conn": conn,
        }

    def _rollback_news_mutation(
        self, state: Dict[str, object], mark_batch_failed: bool = False
    ) -> None:
        """恢复单次 mutation；保留同批次更早的有效本地变更。"""
        token = state["token"]
        self._restore_local_sqlite_snapshot(
            state["path"], state["remote_key"], state["before"]
        )
        if self._batch_mode and state["had_pending"]:
            self._batch_dirty.add(token)
            self._mark_strict_local_dirty(state["remote_key"])
        else:
            self._batch_dirty.discard(token)
            if hasattr(self, "_batch_snapshots"):
                self._batch_snapshots.pop(token, None)
        if self._batch_mode and mark_batch_failed:
            self._batch_failed = True

    def _run_news_mutation(
        self,
        date: Optional[str],
        mutation,
        *,
        strict_upload: bool,
        failure_value,
    ):
        """执行共享 news mutation，并使本地状态与远端提交同成败。"""
        try:
            state = self._begin_news_mutation(date)
        except Exception:
            if strict_upload and self._batch_mode:
                # 即使 mutation 尚未取得自己的 before-image，同批更早的
                # mutation 也绝不能在 caller 错误清理时被提交。
                self._batch_failed = True
            if strict_upload:
                raise
            return failure_value
        try:
            result = mutation(state["conn"])
        except Exception:
            self._rollback_news_mutation(state, mark_batch_failed=True)
            if strict_upload:
                raise
            return failure_value

        if not result:
            self._rollback_news_mutation(
                state, mark_batch_failed=strict_upload
            )
            return failure_value

        try:
            uploaded = self._upload_sqlite(
                state["date"], "news", strict_version=strict_upload
            )
        except Exception:
            self._rollback_news_mutation(state, mark_batch_failed=True)
            if strict_upload:
                raise
            return failure_value
        if not uploaded:
            self._rollback_news_mutation(state, mark_batch_failed=True)
            if strict_upload:
                raise RuntimeError("共享 news 数据库严格上传失败")
            return failure_value
        return result

    def _conditional_put_strict(
        self, remote_key: str, content: bytes, content_type: str
    ) -> tuple:
        """用服务端条件写提交本地 authoritative 内容并验证 PUT provenance。"""
        if not hasattr(self, "_remote_provenance"):
            self._remote_provenance = {}
        if remote_key not in self._remote_provenance:
            self._remote_provenance[remote_key] = (
                self._head_provenance_strict(remote_key)
            )
        baseline = self._remote_provenance[remote_key]
        kwargs = {
            "Bucket": self.bucket_name,
            "Key": remote_key,
            "Body": content,
            "ContentLength": len(content),
            "ContentType": content_type,
        }
        if baseline is None:
            kwargs["IfNoneMatch"] = "*"
        else:
            baseline_etag = baseline[1]
            if not baseline_etag:
                raise RuntimeError(
                    f"远程对象缺少可用于条件写的 ETag: {remote_key}"
                )
            kwargs["IfMatch"] = baseline_etag

        try:
            response = self.s3_client.put_object(**kwargs)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("PreconditionFailed", "412", "ConditionalRequestConflict"):
                raise RuntimeError(
                    f"远程对象版本冲突，条件写失败: {remote_key}"
                ) from exc
            raise
        put_etag = response.get("ETag") or ""
        put_version = response.get("VersionId") or ""
        if not put_etag and not put_version:
            raise RuntimeError(
                f"严格条件写未返回 ETag/VersionId: {remote_key}"
            )
        if baseline is not None and (
            (not put_etag or put_etag == baseline[1])
            and (not put_version or put_version == baseline[0])
        ):
            raise RuntimeError(
                f"严格条件写未返回新的对象版本: {remote_key}"
            )
        confirmed = self._head_provenance_strict(remote_key)
        if confirmed is None:
            raise RuntimeError(f"严格条件写后对象不存在: {remote_key}")
        if (put_etag and confirmed[1] != put_etag) or (
            put_version and confirmed[0] != put_version
        ):
            raise RuntimeError(
                f"严格条件写后对象版本已变化: {remote_key}"
            )
        self._remote_provenance[remote_key] = confirmed
        self._strict_local_authoritative.discard(remote_key)
        return confirmed

    def begin_batch(self):
        """开启批量模式：延迟上传，避免频繁上传同一文件"""
        self._batch_mode = True
        self._batch_dirty.clear()
        self._batch_snapshots = {}
        self._batch_failed = False

    def end_batch(self):
        """结束批量模式：统一上传所有脏数据库"""
        return self._finish_batch(strict=False)

    def abort_batch(self):
        """恢复批次首次 mutation 前镜像，不执行任何远端上传。"""
        self._batch_mode = False
        dirty = set(self._batch_dirty)
        snapshots = dict(getattr(self, "_batch_snapshots", {}))
        failures = []
        try:
            for (date, db_type), before in snapshots.items():
                local_path = self._get_local_db_path(date, db_type)
                remote_key = self._get_remote_db_key(date, db_type)
                try:
                    self._restore_local_sqlite_snapshot(
                        local_path, remote_key, before
                    )
                except Exception as exc:
                    failures.append(exc)

            # 正常 mutation 总会有首镜像；这里仍清理异常中途留下的
            # connection/WAL/dirty，避免后续 strict read 误认本地 authoritative。
            for date, db_type in dirty.difference(snapshots):
                local_path = self._get_local_db_path(date, db_type)
                remote_key = self._get_remote_db_key(date, db_type)
                try:
                    self._close_cached_connection(local_path)
                    for suffix in ("-wal", "-shm"):
                        auxiliary = Path(f"{local_path}{suffix}")
                        if auxiliary.exists():
                            auxiliary.unlink()
                    self._strict_local_authoritative.discard(remote_key)
                except Exception as exc:
                    failures.append(exc)
        finally:
            self._batch_dirty.clear()
            self._batch_snapshots = {}
            self._batch_failed = False
        if failures:
            raise RuntimeError(f"远程数据库批次回滚失败: {failures}")
        return True

    def _finish_batch(self, strict: bool) -> bool:
        """提交批次；失败对象恢复该批次第一次 mutation 前镜像。"""
        self._batch_mode = False
        dirty = list(self._batch_dirty)
        self._batch_dirty.clear()
        snapshots = getattr(self, "_batch_snapshots", {})
        failures = []

        if getattr(self, "_batch_failed", False):
            failures.append("批次内本地 mutation 失败")
            dirty = []

        for date, db_type in dirty:
            try:
                uploaded = self._upload_sqlite(
                    date, db_type, strict_version=strict
                )
            except Exception as exc:
                uploaded = False
                failures.append(exc)
            if uploaded:
                snapshots.pop((date, db_type), None)
            else:
                failures.append((date, db_type))
                before = snapshots.pop((date, db_type), None)
                if before is not None:
                    local_path = self._get_local_db_path(date, db_type)
                    remote_key = self._get_remote_db_key(date, db_type)
                    self._restore_local_sqlite_snapshot(
                        local_path, remote_key, before
                    )

        # 本地 mutation 已失败时没有上传，恢复本批次所有首镜像。
        for (date, db_type), before in list(snapshots.items()):
            local_path = self._get_local_db_path(date, db_type)
            remote_key = self._get_remote_db_key(date, db_type)
            self._restore_local_sqlite_snapshot(
                local_path, remote_key, before
            )
        self._batch_snapshots = {}
        self._batch_failed = False
        if failures and strict:
            raise RuntimeError(f"远程数据库批次上传失败: {failures}")
        return not failures

    def end_batch_strict(self):
        """结束严格批次；任一远程上传或验证失败即上抛。"""
        return self._finish_batch(strict=True)

    def _upload_sqlite(
        self,
        date: Optional[str] = None,
        db_type: str = "news",
        strict_version: bool = False,
    ) -> bool:
        """
        上传本地 SQLite 文件到远程存储

        批量模式下延迟上传，由 end_batch() 统一触发。

        Args:
            date: 日期字符串
            db_type: 数据库类型 ("news" 或 "rss")

        Returns:
            是否上传成功
        """
        local_path = self._get_local_db_path(date, db_type)
        r2_key = self._get_remote_db_key(date, db_type)
        # news/{date}.db 同时承载热榜、AI 状态和 period checkpoint；
        # 所有写者都必须走同一 conditional CAS，不能存在普通 PUT 后门。
        conditional_write = strict_version or db_type == "news"
        if conditional_write:
            self._mark_strict_local_dirty(r2_key)
        if self._batch_mode:
            self._batch_dirty.add((date, db_type))
            return True

        if not local_path.exists():
            print(f"[远程存储] 本地文件不存在，无法上传: {local_path}")
            return False

        try:
            # 获取本地文件大小
            local_size = local_path.stat().st_size
            print(f"[远程存储] 准备上传: {local_path} ({local_size} bytes) -> {r2_key}")

            # 读取文件内容为 bytes 后上传
            # 避免传入文件对象时 requests 库使用 chunked transfer encoding
            # 腾讯云 COS 等 S3 兼容服务可能无法正确处理 chunked encoding
            with open(local_path, 'rb') as f:
                file_content = f.read()

            # 使用 put_object 并明确设置 ContentLength，确保不使用 chunked encoding
            if conditional_write:
                self._conditional_put_strict(
                    r2_key, file_content, "application/x-sqlite3"
                )
            else:
                self.s3_client.put_object(
                    Bucket=self.bucket_name,
                    Key=r2_key,
                    Body=file_content,
                    ContentLength=local_size,
                    ContentType='application/x-sqlite3',
                )
            print(f"[远程存储] 已上传: {local_path} -> {r2_key}")

            if conditional_write:
                # conditional PUT 内部已把 PUT provenance 与最终 HEAD 精确
                # 对齐；不再追加一个无法归属于本轮写入的弱 exists 检查。
                print(f"[远程存储] 上传验证成功: {r2_key}")
                return True

            # 验证上传成功
            if self._check_object_exists(r2_key, strict=strict_version):
                print(f"[远程存储] 上传验证成功: {r2_key}")
                return True
            else:
                print(f"[远程存储] 上传验证失败: 文件未在远程存储中找到")
                return False

        except Exception as e:
            print(f"[远程存储] 上传失败: {e}")
            if strict_version:
                raise
            return False

    def _get_connection(
        self,
        date: Optional[str] = None,
        db_type: str = "news",
        strict_exists: bool = False,
    ) -> sqlite3.Connection:
        """
        获取数据库连接

        Args:
            date: 日期字符串
            db_type: 数据库类型 ("news" 或 "rss")

        Returns:
            数据库连接
        """
        local_path = self._get_local_db_path(date, db_type)
        db_path = str(local_path)
        remote_key = self._get_remote_db_key(date, db_type)
        remote_exists = None

        # news DB 的任一生产写者都必须从绑定的远端 baseline 派生。
        # 普通业务方法仍可在各自异常处理里 fail-soft，但不能读取陈旧
        # 快照后再覆盖 strict AI/checkpoint 状态。
        if db_type == "news":
            strict_exists = True

        if strict_exists:
            remote_exists = self._refresh_remote_sqlite_strict(
                remote_key, local_path
            )

        if db_path not in self._db_connections:
            # 确保目录存在
            local_path.parent.mkdir(parents=True, exist_ok=True)

            # 如果本地不存在，尝试从远程存储下载
            if not local_path.exists():
                if remote_exists is not False:
                    self._download_sqlite(
                        date, db_type, strict_exists=strict_exists
                    )

            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            self._init_tables(conn, db_type)
            self._db_connections[db_path] = conn

        return self._db_connections[db_path]

    def _get_first_seen_ledger_connection(
        self, strict: bool = False
    ) -> sqlite3.Connection:
        local_path = self.temp_dir / "rss" / "first-seen-v1.db"
        remote_key = "rss/first-seen-v1.db"
        if strict:
            self._refresh_remote_sqlite_strict(remote_key, local_path)
        db_path = str(local_path)
        if db_path not in self._db_connections:
            local_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            self._init_first_seen_ledger(conn)
            self._db_connections[db_path] = conn
        return self._db_connections[db_path]

    def _get_rss_source_version_strict(self, date: str) -> str:
        remote_key = self._get_remote_db_key(date, "rss")
        provenance = self._head_provenance_strict(remote_key)
        if provenance is None:
            raise RuntimeError(f"RSS 日库在同步期间消失: {remote_key}")
        return repr(provenance)

    def _list_rss_history_sources_strict(
        self, through_date: str
    ) -> Dict[str, str]:
        sources = {}
        paginator = self.s3_client.get_paginator("list_objects_v2")
        for page in paginator.paginate(
            Bucket=self.bucket_name, Prefix="rss/"
        ):
            for obj in page.get("Contents", []):
                match = re.fullmatch(
                    r"rss/(\d{4}-\d{2}-\d{2})\.db", obj["Key"]
                )
                if match and match.group(1) <= through_date:
                    date = match.group(1)
                    sources[date] = self._get_rss_source_version_strict(date)
        return sources

    def _upload_first_seen_ledger_strict(self) -> None:
        local_path = self.temp_dir / "rss" / "first-seen-v1.db"
        remote_key = "rss/first-seen-v1.db"
        content = local_path.read_bytes()
        self._mark_strict_local_dirty(remote_key)
        self._conditional_put_strict(
            remote_key, content, "application/x-sqlite3"
        )
        self._first_seen_needs_upload = False

    def _persist_first_seen_ledger_strict(self) -> None:
        if getattr(self, "_first_seen_needs_upload", False):
            self._upload_first_seen_ledger_strict()

    def _get_rss_connection(
        self, date: Optional[str] = None, strict: bool = False
    ) -> sqlite3.Connection:
        """严格 RSS 读取时区分真实 404 与远程访问故障。"""
        return self._get_connection(
            date, db_type="rss", strict_exists=strict
        )

    def _get_ai_connection(
        self, date: Optional[str] = None, strict: bool = False
    ) -> sqlite3.Connection:
        """严格 AI 读取时区分真实 404 与远程访问故障。"""
        return self._get_connection(
            date, db_type="news", strict_exists=strict
        )

    # ========================================
    # StorageBackend 接口实现（委托给 mixin + 上传）
    # ========================================

    def save_news_data(self, data: NewsData) -> bool:
        """
        保存新闻数据到远程存储

        流程：下载现有数据库 → 插入/更新数据 → 上传回远程存储
        """
        local_path = self._get_local_db_path(data.date, "news")
        remote_key = self._get_remote_db_key(data.date, "news")
        try:
            # 先严格刷新并绑定 baseline；后续提交只允许 conditional CAS。
            conn = self._get_connection(
                data.date, "news", strict_exists=True
            )
            before = local_path.read_bytes()
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) as count FROM news_items")
            row = cursor.fetchone()
            existing_count = row[0] if row else 0
            if existing_count > 0:
                print(
                    f"[远程存储] 已有 {existing_count} 条历史记录，"
                    "将合并新数据"
                )

            (
                success,
                new_count,
                updated_count,
                title_changed_count,
                off_list_count,
            ) = self._save_news_data_impl(
                data, "[远程存储]", conn=conn
            )
            if not success:
                self._restore_local_sqlite_snapshot(
                    local_path, remote_key, before
                )
                return False

            cursor.execute("SELECT COUNT(*) as count FROM news_items")
            row = cursor.fetchone()
            final_count = row[0] if row else 0
            log_parts = [f"[远程存储] 处理完成：新增 {new_count} 条"]
            if updated_count > 0:
                log_parts.append(f"更新 {updated_count} 条")
            if title_changed_count > 0:
                log_parts.append(f"标题变更 {title_changed_count} 条")
            if off_list_count > 0:
                log_parts.append(f"脱榜 {off_list_count} 条")
            log_parts.append(f"(去重后总计: {final_count} 条)")
            print("，".join(log_parts))

            if not self._upload_sqlite(data.date, "news"):
                raise RuntimeError("共享 news 数据库 CAS 上传失败")
            print("[远程存储] 数据已同步到远程存储")
            return True
        except Exception as exc:
            print(f"[远程存储] 热榜数据严格保存失败: {exc}")
            if "before" in locals():
                self._restore_local_sqlite_snapshot(
                    local_path, remote_key, before
                )
            return False

    def get_today_all_data(self, date: Optional[str] = None) -> Optional[NewsData]:
        """获取指定日期的所有新闻数据（合并后）"""
        return self._get_today_all_data_impl(date)

    def get_latest_crawl_data(self, date: Optional[str] = None) -> Optional[NewsData]:
        """获取最新一次抓取的数据"""
        return self._get_latest_crawl_data_impl(date)

    def detect_new_titles(self, current_data: NewsData) -> Dict[str, Dict]:
        """检测新增的标题"""
        return self._detect_new_titles_impl(current_data)

    def is_first_crawl_today(self, date: Optional[str] = None) -> bool:
        """检查是否是当天第一次抓取"""
        return self._is_first_crawl_today_impl(date)

    # ========================================
    # 时间段执行记录（调度系统）
    # ========================================

    def has_period_executed(self, date_str: str, period_key: str, action: str) -> bool:
        """检查指定时间段的某个 action 是否已执行"""
        return self._has_period_executed_impl(date_str, period_key, action)

    def has_period_executed_strict(
        self, date_str: str, period_key: str, action: str
    ) -> bool:
        """严格刷新共享 news DB 后读取执行状态。"""
        return self._has_period_executed_impl(
            date_str, period_key, action, strict_read=True
        )

    def record_period_execution(self, date_str: str, period_key: str, action: str) -> bool:
        """记录时间段的 action 执行"""
        success = self._run_news_mutation(
            date_str,
            lambda conn: self._record_period_execution_impl(
                date_str, period_key, action, conn=conn
            ),
            strict_upload=False,
            failure_value=False,
        )
        if success:
            now_str = self._get_configured_time().strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            print(
                f"[远程存储] 时间段执行记录已同步: "
                f"{period_key}/{action} at {now_str}"
            )
        return bool(success)

    def record_period_execution_strict(
        self, date_str: str, period_key: str, action: str
    ) -> bool:
        """事务写入本地后以 conditional PUT 提交；冲突时恢复本地前镜像。"""
        try:
            return bool(self._run_news_mutation(
                date_str,
                lambda conn: self._record_period_execution_impl(
                    date_str, period_key, action, conn=conn
                ),
                strict_upload=True,
                failure_value=False,
            ))
        except Exception as exc:
            print(f"[远程存储] 严格时间段执行记录失败: {exc}")
            return False

    def get_latest_period_execution(
        self, period_key: str, action: str, through_date: str
    ) -> Optional[str]:
        """跨远程每日数据库读取截止日期内最近一次成功执行时间。"""
        paginator = self.s3_client.get_paginator("list_objects_v2")
        dates = set()
        for page in paginator.paginate(Bucket=self.bucket_name, Prefix="news/"):
            for obj in page.get("Contents", []):
                match = re.fullmatch(r"news/(\d{4}-\d{2}-\d{2})\.db", obj["Key"])
                if not match:
                    continue
                date_str = match.group(1)
                try:
                    datetime.strptime(date_str, "%Y-%m-%d")
                except ValueError:
                    continue
                if date_str <= through_date:
                    dates.add(date_str)

        for date_str in sorted(dates, reverse=True):
            executed_at = self._get_period_execution_at_impl(
                date_str, period_key, action, strict_read=True
            )
            if executed_at is not None:
                return executed_at
        return None

    def get_latest_period_execution_strict(
        self, period_key: str, action: str, through_date: str
    ) -> Optional[str]:
        """按远端 provenance 严格读取最近成功执行时间。"""
        return self.get_latest_period_execution(
            period_key, action, through_date
        )

    # ========================================
    # RSS 数据存储方法
    # ========================================

    def save_rss_data(self, data: RSSData) -> bool:
        """
        保存 RSS 数据到远程存储

        流程：下载现有数据库 → 插入/更新数据 → 上传回远程存储
        """
        remote_key = self._get_remote_db_key(data.date, "rss")
        try:
            self._consume_first_seen_outboxes_strict(data.date)
        except Exception as exc:
            print(f"[远程存储] RSS first-seen 保存前恢复失败: {exc}")
            return False
        try:
            # 保存前绑定 authoritative 远端版本；后续账本回填不得重新下载
            # 覆盖本轮尚未上传的本地修改。
            self._get_rss_connection(data.date, strict=True)
        except Exception as exc:
            print(f"[远程存储] RSS 严格版本绑定失败: {exc}")
            return False

        success, new_count, updated_count = self._save_rss_data_impl(data, "[远程存储]")

        if not success:
            return False
        if not hasattr(self, "_strict_local_authoritative"):
            self._strict_local_authoritative = set()
        self._strict_local_authoritative.add(remote_key)

        # 输出统计日志
        log_parts = [f"[远程存储] RSS 处理完成：新增 {new_count} 条"]
        if updated_count > 0:
            log_parts.append(f"更新 {updated_count} 条")
        print("，".join(log_parts))

        # 上传到远程存储
        try:
            uploaded = self._upload_sqlite(
                data.date, db_type="rss", strict_version=True
            )
        except Exception as exc:
            print(f"[远程存储] RSS 上传版本校验失败: {exc}")
            return False
        if not uploaded:
            print(f"[远程存储] RSS 上传远程存储失败")
            return False
        try:
            # raw/outbox 已经 durable CAS 提交后才消费账本；因此即使账本上传
            # 失败，新进程也能从远端日库 outbox 恢复。
            self._sync_first_seen_ledger_strict(data)
        except Exception as exc:
            print(f"[远程存储] RSS first-seen 账本上传失败: {exc}")
            return False
        print(f"[远程存储] RSS 数据及 first-seen 账本已同步")
        return True

    def get_rss_data(self, date: Optional[str] = None) -> Optional[RSSData]:
        """获取指定日期的所有 RSS 数据"""
        return self._get_rss_data_impl(date)

    def get_rss_data_strict(
        self, date: Optional[str] = None
    ) -> Optional[RSSData]:
        """严格获取 RSS 数据，远程访问和下载异常向上抛出。"""
        return self._get_rss_data_impl(date, strict=True)

    def get_rss_feed_statuses(
        self, date: Optional[str] = None
    ) -> Dict[str, str]:
        """获取指定日库中每个 RSS 源的最新抓取状态。"""
        return self._get_rss_feed_statuses_impl(date)

    def get_rss_feed_statuses_strict(
        self, date: Optional[str] = None
    ) -> Dict[str, str]:
        """严格获取 RSS 来源状态，远程访问异常向上抛出。"""
        return self._get_rss_feed_statuses_impl(date, strict=True)

    def detect_new_rss_items(self, current_data: RSSData) -> Dict[str, List[RSSItem]]:
        """检测新增的 RSS 条目"""
        return self._detect_new_rss_items_impl(current_data)

    def get_latest_rss_data(self, date: Optional[str] = None) -> Optional[RSSData]:
        """获取最新一次抓取的 RSS 数据"""
        return self._get_latest_rss_data_impl(date)

    # ========================================
    # AI 智能筛选存储方法
    # ========================================

    def get_active_ai_filter_tags(self, date=None, interests_file="ai_interests.txt"):
        return self._get_active_tags_impl(date, interests_file)

    def get_ai_filter_tag_snapshot_strict(
        self, date=None, interests_file="ai_interests.txt"
    ):
        return self._get_ai_filter_tag_snapshot_strict_impl(
            date, interests_file
        )

    def replace_ai_filter_tags_strict(
        self,
        tags,
        version,
        prompt_hash,
        date=None,
        interests_file="ai_interests.txt",
    ):
        return self._run_news_mutation(
            date,
            lambda conn: self._replace_ai_filter_tags_strict_impl(
                date,
                tags,
                version,
                prompt_hash,
                interests_file,
                conn=conn,
            ),
            strict_upload=True,
            failure_value=None,
        )

    def get_latest_prompt_hash(self, date=None, interests_file="ai_interests.txt"):
        return self._get_latest_prompt_hash_impl(date, interests_file)

    def get_latest_ai_filter_tag_version(self, date=None):
        return self._get_latest_tag_version_impl(date)

    def deprecate_all_ai_filter_tags(self, date=None, interests_file="ai_interests.txt"):
        return self._run_news_mutation(
            date,
            lambda _conn: self._deprecate_all_tags_impl(
                date, interests_file
            ),
            strict_upload=False,
            failure_value=0,
        )

    def save_ai_filter_tags(self, tags, version, prompt_hash, date=None, interests_file="ai_interests.txt"):
        return self._run_news_mutation(
            date,
            lambda _conn: self._save_tags_impl(
                date, tags, version, prompt_hash, interests_file
            ),
            strict_upload=False,
            failure_value=0,
        )

    def save_ai_filter_results(self, results, date=None):
        return self._run_news_mutation(
            date,
            lambda _conn: self._save_filter_results_impl(date, results),
            strict_upload=False,
            failure_value=0,
        )

    def get_active_ai_filter_results(self, date=None, interests_file="ai_interests.txt"):
        return self._get_active_filter_results_impl(date, interests_file)

    def get_active_ai_filter_results_strict(self, date=None, interests_file="ai_interests.txt"):
        return self._get_active_filter_results_impl(
            date, interests_file, strict=True
        )

    def deprecate_specific_ai_filter_tags(self, tag_ids, date=None):
        return self._run_news_mutation(
            date,
            lambda _conn: self._deprecate_specific_tags_impl(date, tag_ids),
            strict_upload=False,
            failure_value=0,
        )

    def update_ai_filter_tags_hash(self, interests_file, new_hash, date=None):
        return self._run_news_mutation(
            date,
            lambda _conn: self._update_tags_hash_impl(
                date, interests_file, new_hash
            ),
            strict_upload=False,
            failure_value=0,
        )

    def update_ai_filter_tag_descriptions(self, tag_updates, date=None, interests_file="ai_interests.txt"):
        return self._run_news_mutation(
            date,
            lambda _conn: self._update_tag_descriptions_impl(
                date, tag_updates, interests_file
            ),
            strict_upload=False,
            failure_value=0,
        )

    def update_ai_filter_tag_priorities(self, tag_priorities, date=None, interests_file="ai_interests.txt"):
        return self._run_news_mutation(
            date,
            lambda _conn: self._update_tag_priorities_impl(
                date, tag_priorities, interests_file
            ),
            strict_upload=False,
            failure_value=0,
        )

    def save_analyzed_news(self, news_ids, source_type, interests_file, prompt_hash, matched_ids, date=None):
        return self._run_news_mutation(
            date,
            lambda _conn: self._save_analyzed_news_impl(
                date,
                news_ids,
                source_type,
                interests_file,
                prompt_hash,
                matched_ids,
            ),
            strict_upload=False,
            failure_value=0,
        )

    def get_analyzed_news_ids(self, source_type="hotlist", date=None, interests_file="ai_interests.txt"):
        return self._get_analyzed_news_ids_impl(date, source_type, interests_file)

    def get_analyzed_news_ids_strict(self, source_type="hotlist", date=None, interests_file="ai_interests.txt"):
        return self._get_analyzed_news_ids_impl(
            date, source_type, interests_file, strict=True
        )

    def replace_ai_filter_batch_strict(
        self,
        results,
        succeeded_news_ids,
        succeeded_rss_ids,
        interests_file,
        prompt_hash,
        date=None,
    ):
        return self._run_news_mutation(
            date,
            lambda conn: self._replace_ai_filter_batch_strict_impl(
                date,
                results,
                succeeded_news_ids,
                succeeded_rss_ids,
                interests_file,
                prompt_hash,
                conn=conn,
            ),
            strict_upload=True,
            failure_value=None,
        )

    def clear_analyzed_news(self, date=None, interests_file="ai_interests.txt"):
        return self._run_news_mutation(
            date,
            lambda _conn: self._clear_analyzed_news_impl(
                date, interests_file
            ),
            strict_upload=False,
            failure_value=0,
        )

    def clear_unmatched_analyzed_news(self, date=None, interests_file="ai_interests.txt"):
        return self._run_news_mutation(
            date,
            lambda _conn: self._clear_unmatched_analyzed_news_impl(
                date, interests_file
            ),
            strict_upload=False,
            failure_value=0,
        )

    def get_all_news_ids(self, date=None):
        return self._get_all_news_ids_impl(date)

    def get_all_rss_ids(self, date=None):
        return self._get_all_rss_ids_impl(date)

    def get_all_rss_ids_strict(self, date=None):
        return self._get_all_rss_ids_impl(date, strict=True)

    def get_earliest_rss_discoveries_strict(
        self, candidate_identities, through_date
    ):
        return self._query_first_seen_ledger_strict(
            set(candidate_identities), through_date
        )

    # ========================================
    # 远程特有功能：TXT/HTML 快照（临时目录）
    # ========================================

    def save_txt_snapshot(self, data: NewsData) -> Optional[str]:
        """保存 TXT 快照（远程存储模式下默认不支持）"""
        if not self.enable_txt:
            return None

        # 如果启用，保存到本地临时目录
        try:
            date_folder = self._format_date_folder(data.date)
            txt_dir = self.temp_dir / date_folder / "txt"
            txt_dir.mkdir(parents=True, exist_ok=True)

            file_path = txt_dir / f"{data.crawl_time}.txt"

            with open(file_path, "w", encoding="utf-8") as f:
                for source_id, news_list in data.items.items():
                    source_name = data.id_to_name.get(source_id, source_id)

                    if source_name and source_name != source_id:
                        f.write(f"{source_id} | {source_name}\n")
                    else:
                        f.write(f"{source_id}\n")

                    sorted_news = sorted(news_list, key=lambda x: x.rank)

                    for item in sorted_news:
                        line = f"{item.rank}. {item.title}"
                        if item.url:
                            line += f" [URL:{item.url}]"
                        if item.mobile_url:
                            line += f" [MOBILE:{item.mobile_url}]"
                        f.write(line + "\n")

                    f.write("\n")

                if data.failed_ids:
                    f.write("==== 以下ID请求失败 ====\n")
                    for failed_id in data.failed_ids:
                        f.write(f"{failed_id}\n")

            print(f"[远程存储] TXT 快照已保存: {file_path}")
            return str(file_path)

        except Exception as e:
            print(f"[远程存储] 保存 TXT 快照失败: {e}")
            return None

    def save_html_report(self, html_content: str, filename: str) -> Optional[str]:
        """保存 HTML 报告到临时目录"""
        if not self.enable_html:
            return None

        try:
            date_folder = self._format_date_folder()
            html_dir = self.temp_dir / date_folder / "html"
            html_dir.mkdir(parents=True, exist_ok=True)

            file_path = html_dir / filename

            with open(file_path, "w", encoding="utf-8") as f:
                f.write(html_content)

            print(f"[远程存储] HTML 报告已保存: {file_path}")
            return str(file_path)

        except Exception as e:
            print(f"[远程存储] 保存 HTML 报告失败: {e}")
            return None

    # ========================================
    # 远程特有功能：资源清理
    # ========================================

    def cleanup(self) -> None:
        """清理资源（关闭连接和删除临时文件）"""
        # 检查 Python 是否正在关闭
        if sys.meta_path is None:
            return

        # 关闭数据库连接
        db_connections = getattr(self, "_db_connections", {})
        for db_path, conn in list(db_connections.items()):
            try:
                conn.close()
                print(f"[远程存储] 关闭数据库连接: {db_path}")
            except Exception as e:
                print(f"[远程存储] 关闭连接失败 {db_path}: {e}")

        if db_connections:
            db_connections.clear()

        # 删除临时目录
        temp_dir = getattr(self, "temp_dir", None)
        if temp_dir:
            try:
                if temp_dir.exists():
                    shutil.rmtree(temp_dir)
                    print(f"[远程存储] 临时目录已清理: {temp_dir}")
            except Exception as e:
                # 忽略 Python 关闭时的错误
                if sys.meta_path is not None:
                    print(f"[远程存储] 清理临时目录失败: {e}")

        downloaded_files = getattr(self, "_downloaded_files", None)
        if downloaded_files:
            downloaded_files.clear()

    def cleanup_old_data(self, retention_days: int) -> int:
        """
        清理远程存储上的过期数据

        Args:
            retention_days: 保留天数（0 表示不清理）

        Returns:
            删除的数据库文件数量
        """
        if retention_days <= 0:
            return 0

        deleted_count = 0
        cutoff_date = self._get_configured_time() - timedelta(days=retention_days)

        try:
            # 列出远程存储中 news/ 前缀下的所有对象
            paginator = self.s3_client.get_paginator('list_objects_v2')
            pages = paginator.paginate(Bucket=self.bucket_name, Prefix="news/")

            # 收集需要删除的对象键
            objects_to_delete = []
            deleted_dates = set()

            for page in pages:
                if 'Contents' not in page:
                    continue

                for obj in page['Contents']:
                    key = obj['Key']

                    # 解析日期（格式: news/YYYY-MM-DD.db）
                    folder_date = None
                    date_str = None
                    try:
                        date_match = re.match(r'news/(\d{4})-(\d{2})-(\d{2})\.db$', key)
                        if date_match:
                            folder_date = datetime(
                                int(date_match.group(1)),
                                int(date_match.group(2)),
                                int(date_match.group(3)),
                                tzinfo=pytz.timezone(self.timezone)
                            )
                            date_str = f"{date_match.group(1)}-{date_match.group(2)}-{date_match.group(3)}"
                    except Exception:
                        continue

                    if folder_date and folder_date < cutoff_date:
                        objects_to_delete.append({'Key': key})
                        deleted_dates.add(date_str)

            # 批量删除对象（每次最多 1000 个）
            if objects_to_delete:
                batch_size = 1000
                for i in range(0, len(objects_to_delete), batch_size):
                    batch = objects_to_delete[i:i + batch_size]
                    try:
                        self.s3_client.delete_objects(
                            Bucket=self.bucket_name,
                            Delete={'Objects': batch}
                        )
                        print(f"[远程存储] 删除 {len(batch)} 个对象")
                    except Exception as e:
                        print(f"[远程存储] 批量删除失败: {e}")

                deleted_count = len(deleted_dates)
                for date_str in sorted(deleted_dates):
                    print(f"[远程存储] 清理过期数据: news/{date_str}.db")

                print(f"[远程存储] 共清理 {deleted_count} 个过期日期数据库文件")

            return deleted_count

        except Exception as e:
            print(f"[远程存储] 清理过期数据失败: {e}")
            return deleted_count

    def __del__(self):
        """析构函数"""
        # 检查 Python 是否正在关闭
        if sys.meta_path is None:
            return
        try:
            self.cleanup()
        except Exception:
            # Python 关闭时可能会出错，忽略即可
            pass

    # ========================================
    # 远程特有功能：数据拉取和列表
    # ========================================

    def pull_recent_days(self, days: int, local_data_dir: str = "output") -> int:
        """
        从远程拉取最近 N 天的数据到本地

        Args:
            days: 拉取天数
            local_data_dir: 本地数据目录

        Returns:
            成功拉取的数据库文件数量
        """
        if days <= 0:
            return 0

        local_dir = Path(local_data_dir)
        local_dir.mkdir(parents=True, exist_ok=True)

        pulled_count = 0
        now = self._get_configured_time()

        print(f"[远程存储] 开始拉取最近 {days} 天的数据...")

        for i in range(days):
            date = now - timedelta(days=i)
            date_str = date.strftime("%Y-%m-%d")

            # 本地目标路径
            local_date_dir = local_dir / date_str
            local_db_path = local_date_dir / "news.db"

            # 如果本地已存在，跳过
            if local_db_path.exists():
                print(f"[远程存储] 跳过（本地已存在）: {date_str}")
                continue

            # 远程对象键
            remote_key = f"news/{date_str}.db"

            # 检查远程是否存在
            if not self._check_object_exists(remote_key):
                print(f"[远程存储] 跳过（远程不存在）: {date_str}")
                continue

            # 下载（使用 get_object + iter_chunks 处理 chunked encoding）
            try:
                local_date_dir.mkdir(parents=True, exist_ok=True)
                response = self.s3_client.get_object(Bucket=self.bucket_name, Key=remote_key)
                with open(local_db_path, 'wb') as f:
                    for chunk in response['Body'].iter_chunks(chunk_size=1024*1024):
                        f.write(chunk)
                print(f"[远程存储] 已拉取: {remote_key} -> {local_db_path}")
                pulled_count += 1
            except Exception as e:
                print(f"[远程存储] 拉取失败 ({date_str}): {e}")

        print(f"[远程存储] 拉取完成，共下载 {pulled_count} 个数据库文件")
        return pulled_count

    def list_remote_dates(self) -> List[str]:
        """
        列出远程存储中所有可用的日期

        Returns:
            日期字符串列表（YYYY-MM-DD 格式）
        """
        dates = []

        try:
            paginator = self.s3_client.get_paginator('list_objects_v2')
            pages = paginator.paginate(Bucket=self.bucket_name, Prefix="news/")

            for page in pages:
                if 'Contents' not in page:
                    continue

                for obj in page['Contents']:
                    key = obj['Key']
                    # 解析日期
                    date_match = re.match(r'news/(\d{4}-\d{2}-\d{2})\.db$', key)
                    if date_match:
                        dates.append(date_match.group(1))

            return sorted(dates, reverse=True)

        except Exception as e:
            print(f"[远程存储] 列出远程日期失败: {e}")
            return []

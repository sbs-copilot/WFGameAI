"""
GitLab Service
GitLab服务封装类，提供统一的GitLab操作接口

Features:
- 仓库验证、克隆、分支获取
- 高效文件下载（支持指定文件类型过滤）
- 并发下载优化
- 错误处理和重试机制
- 进度跟踪和统计
- 可扩展的配置系统
"""

import os
import re
import time
import shutil
import logging
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Callable
from urllib.parse import quote, urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from enum import Enum
import requests
import hashlib


# 单文件调试时：设置 logger 输出到 控制台
# logging.basicConfig(
#     level=logging.INFO,
#     format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
#     handlers=[
#         logging.StreamHandler(),
#     ],
# )

logger = logging.getLogger(__name__)


class DownloadStrategy(Enum):
    """下载策略枚举"""

    AUTO = "auto"  # 自动选择
    GIT_CLONE = "git_clone"  # 完整克隆
    GITLAB_API = "gitlab_api"  # API下载


@dataclass
class GitLabConfig:
    """GitLab配置类"""

    repo_url: str
    gitlab_host: Optional[str] = None  # GitLab实例地址
    project_path: Optional[str] = None  # 项目路径
    access_token: Optional[str] = None
    max_workers: int = 0  # 并发线程数，0表示自动检测
    verbose_logging: bool = True
    timeout: int = 600
    retry_attempts: int = 3
    retry_delay: float = 1.0
    chunk_size: int = 8192
    skip_ssl_verify: bool = False
    per_page: int = 100


@dataclass
class DownloadConfig:
    """下载配置类"""

    file_extensions: List[str] = field(
        default_factory=lambda: ["jpg", "jpeg", "png", "bmp", "tiff", "tif", "webp"]
    )
    include_patterns: List[str] = field(default_factory=list)  # 包含模式
    exclude_patterns: List[str] = field(default_factory=list)  # 排除模式
    preserve_structure: bool = True  # 保持目录结构
    overwrite_existing: bool = False  # 覆盖已存在文件
    strategy: DownloadStrategy = DownloadStrategy.AUTO  # 下载策略


@dataclass
class DownloadStats:
    """下载统计信息"""

    total_files: int = 0
    filtered_files: int = 0
    downloaded_files: int = 0
    failed_files: int = 0
    skipped_files: int = 0
    total_size: int = 0
    downloaded_size: int = 0
    start_time: Optional[float] = None
    end_time: Optional[float] = None

    @property
    def duration(self) -> float:
        """计算耗时"""
        if self.start_time and self.end_time:
            return self.end_time - self.start_time
        return 0.0

    @property
    def success_rate(self) -> float:
        """计算成功率"""
        if self.filtered_files == 0:
            return 0.0
        return (self.downloaded_files / self.filtered_files) * 100

    @property
    def average_speed(self) -> float:
        """计算平均下载速度（MB/s）"""
        if self.duration == 0:
            return 0.0
        return (self.downloaded_size / 1024 / 1024) / self.duration


@dataclass
class FileResult:
    """单个文件操作结果"""

    file_path: str
    local_path: Optional[str] = None
    size: int = 0
    error: Optional[str] = None
    success: bool = True


@dataclass
class DownloadResult:
    """下载操作结果"""

    success: bool
    message: Optional[str] = None
    error: Optional[str] = None
    stats: Optional[DownloadStats] = None
    successful_files: List[FileResult] = field(default_factory=list)
    failed_files: List[FileResult] = field(default_factory=list)
    output_dir: Optional[str] = None

    @property
    def total_successful(self) -> int:
        """成功文件数量"""
        return len(self.successful_files)

    @property
    def total_failed(self) -> int:
        """失败文件数量"""
        return len(self.failed_files)


@dataclass
class RepositoryInfo:
    """仓库信息"""

    size: int = 0
    default_branch: str = "main"
    file_count: int = 0
    last_activity: Optional[str] = None
    web_url: Optional[str] = None


@dataclass
class ValidationResult:
    """验证结果"""

    valid: bool
    error: Optional[str] = None
    message: Optional[str] = None


@dataclass
class CloneResult:
    """克隆结果"""

    success: bool
    path: Optional[str] = None
    error: Optional[str] = None
    message: Optional[str] = None


class GitLabService:
    """
    企业级GitLab服务类
    提供统一的GitLab仓库操作、文件下载等功能
    """

    def __init__(self, config: GitLabConfig):
        """
        初始化GitLab服务

        Args:
            config: GitLab配置对象
        """
        if not config.repo_url:
            raise ValueError("repo_url不能为空")

        if not config.gitlab_host:
            config.gitlab_host, config.project_path = self._parse_project_info(
                config.repo_url
            )

        # 如果 max_workers 为 0，则自动检测最优值
        if config.max_workers == 0:
            config.max_workers = self._get_optimal_max_workers()
            logger.info(f"自动设置线程数为: {config.max_workers}")

        self.config = config
        self.session = self._create_session()
        self.stats = DownloadStats()
        self._setup_logging()
        # 初始化编译后的正则表达式缓存
        self._compiled_patterns = {"include": [], "exclude": []}

    @staticmethod
    def parse_gitlab_url(repo_url: str) -> Tuple[str, str]:
        """
        静态方法：解析GitLab仓库URL，支持SSH和HTTPS两种格式

        Args:
            repo_url: 仓库URL，支持格式：
                     - SSH: git@hostname:namespace/project.git
                     - HTTPS: https://hostname/namespace/project.git

        Returns:
            (gitlab_host, project_path)
        """
        try:
            # 移除.git后缀
            if repo_url.endswith(".git"):
                repo_url = repo_url[:-4]

            # 检查是否为SSH格式 (git@hostname:namespace/project)
            if repo_url.startswith("git@"):
                # SSH格式解析: git@hostname:namespace/project
                # 提取主机名和路径
                ssh_pattern = r"git@([^:]+):(.+)"
                match = re.match(ssh_pattern, repo_url)
                if not match:
                    raise ValueError("无效的SSH格式")

                hostname = match.group(1)
                project_path = match.group(2)
                gitlab_host = f"https://{hostname}"

                # 验证项目路径格式 (应该至少包含 namespace/project)
                path_parts = project_path.split("/")
                if len(path_parts) < 2:
                    raise ValueError("项目路径格式错误，应为 namespace/project")

                # SSH格式保持完整路径，但至少取最后两部分
                if len(path_parts) >= 2:
                    project_path = "/".join(path_parts[-2:])

                return gitlab_host, project_path

            # HTTPS格式解析
            else:
                parsed = urlparse(repo_url)
                if not parsed.scheme or not parsed.netloc:
                    raise ValueError("无效的HTTPS URL格式")

                gitlab_host = f"{parsed.scheme}://{parsed.netloc}"

                # 提取项目路径
                path_parts = parsed.path.strip("/").split("/")
                if len(path_parts) < 2:
                    raise ValueError("项目路径格式错误，应为 namespace/project")

                # 取最后两部分作为项目路径 (namespace/project)
                project_path = "/".join(path_parts[-2:])
                return gitlab_host, project_path

        except Exception as e:
            logger.error(f"解析项目信息失败: {e}")
            raise ValueError(f"无效的GitLab URL: {repo_url}")

    def _create_session(self) -> requests.Session:
        """创建HTTP会话"""
        session = requests.Session()

        # 设置请求头
        headers = {"User-Agent": "WFGameAI-GitLab/1.0", "Accept": "application/json"}

        if self.config.access_token:
            headers["PRIVATE-TOKEN"] = self.config.access_token

        session.headers.update(headers)
        session.timeout = self.config.timeout

        # SSL验证配置
        if self.config.skip_ssl_verify:
            session.verify = False
            import urllib3

            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        return session

    def _setup_logging(self):
        """设置日志"""
        if self.config.verbose_logging:
            logger.setLevel(logging.INFO)

    def _parse_project_info(self, repo_url: str) -> Tuple[str, str]:
        """
        解析项目信息（实例方法，调用静态方法）

        Args:
            repo_url: 仓库URL

        Returns:
            (gitlab_host, project_path)
        """
        return self.parse_gitlab_url(repo_url)

    def _get_api_url(self, project_path: str, endpoint: str = "") -> str:
        """构建API URL"""
        encoded_project = quote(project_path, safe="")
        base_url = (
            f"{self.config.gitlab_host.rstrip('/')}/api/v4/projects/{encoded_project}"
        )

        if endpoint:
            return f"{base_url}/{endpoint.lstrip('/')}"
        return base_url

    def _request_with_retry(self, method: str, url: str, **kwargs) -> requests.Response:
        """
        带重试机制的请求

        Args:
            method: HTTP方法
            url: 请求URL
            **kwargs: 其他请求参数

        Returns:
            响应对象
        """
        last_exception = None

        for attempt in range(self.config.retry_attempts):
            try:
                response = self.session.request(method, url, **kwargs)
                response.raise_for_status()
                return response

            except requests.exceptions.RequestException as e:
                last_exception = e
                logger.warning(
                    f"请求失败 (尝试 {attempt + 1}/{self.config.retry_attempts}): {e}"
                )

                if attempt < self.config.retry_attempts - 1:
                    time.sleep(self.config.retry_delay * (2**attempt))  # 指数退避

        raise last_exception

    def validate_repository(self) -> ValidationResult:
        """
        验证仓库是否有效

        Returns:
            验证结果
        """
        try:
            api_url = self._get_api_url(self.config.project_path)
            response = self._request_with_retry("GET", api_url)

            if response.status_code == 200:
                return ValidationResult(valid=True, message="仓库验证成功")
            else:
                return ValidationResult(
                    valid=False, error=f"HTTP状态码: {response.status_code}"
                )

        except Exception as e:
            logger.error(f"验证仓库失败: {e}")
            return ValidationResult(valid=False, error=str(e))

    def get_repository_branches(self) -> List[str]:
        """
        获取仓库分支列表

        Returns:
            分支列表
        """
        try:
            api_url = self._get_api_url(self.config.project_path, "repository/branches")
            response = self._request_with_retry(
                "GET", api_url, params={"per_page": self.config.per_page}
            )

            branches = []
            for branch_info in response.json():
                branches.append(branch_info["name"])

            logger.info(f"获取到 {len(branches)} 个分支")
            return branches

        except Exception as e:
            logger.error(f"获取分支失败: {e}")

    def _should_download_file(
        self, file_info: Dict, download_config: DownloadConfig
    ) -> bool:
        """
        优化的文件过滤方法，使用预编译正则和缓存

        Args:
            file_info:
            {
                "id": "7d70e02340bac451f281cecf0a980907974bd8be",
                "name": "whitespace",
                "type": "blob",
                "path": "files/whitespace",
                "mode": "100644"
            }
            download_config: 下载配置

        Returns:
            是否应该下载
        """
        # 快速检查类型是否为 blob（文件）
        if file_info.get("type") != "blob":
            return False

        file_path = file_info.get("path", "")
        # 使用预编译的排除模式（优先检查，提前返回）
        if self._compiled_patterns["exclude"]:
            for pattern in self._compiled_patterns["exclude"]:
                if pattern.search(file_path):
                    return False

        # 检查文件扩展名（使用缓存的集合）
        if self._allowed_extensions:
            if self._allow_all_extensions:
                return True
            file_ext = Path(file_path).suffix.lower().lstrip(".")
            return file_ext in self._allowed_extensions

        # 使用预编译的包含模式
        if self._compiled_patterns["include"]:
            return any(
                pattern.search(file_path)
                for pattern in self._compiled_patterns["include"]
            )

        return True

    def _download_single_file(
        self,
        file_info: Dict,
        output_dir: str,
        branch: str,
        download_config: DownloadConfig,
    ) -> Tuple[bool, str, Optional[str]]:
        """
        下载单个文件，支持 Git LFS 文件

        Args:
            file_info: 文件信息，包含id(blob SHA)、path、type等字段
            output_dir: 输出目录
            branch: 分支名（仅在回退到路径方式时使用）
            download_config: 下载配置

        Returns:
            (是否成功, 文件路径, 错误信息)
        """
        file_path = file_info.get("path", "")

        try:
            # 构建本地路径
            if download_config.preserve_structure:
                local_path = Path(output_dir) / file_path
            else:
                local_path = Path(output_dir) / Path(file_path).name

            # 检查文件是否已存在
            if local_path.exists() and not download_config.overwrite_existing:
                self.stats.skipped_files += 1
                return True, str(local_path), None

            # 创建目录
            local_path.parent.mkdir(parents=True, exist_ok=True)

            # 首先尝试下载文件内容
            blob_sha = file_info.get("id")
            if not blob_sha:
                return False, file_path, "文件信息中缺少blob SHA"

            # 使用blob SHA方式下载
            api_url = f"{self.config.gitlab_host.rstrip('/')}/api/v4/projects/{quote(self.config.project_path, safe='')}/repository/blobs/{blob_sha}/raw"
            headers = {}
            if self.config.access_token:
                headers["PRIVATE-TOKEN"] = self.config.access_token

            # 下载文件内容
            response = self._request_with_retry(
                "GET", api_url, headers=headers, stream=True
            )

            # 读取前几行检查是否为 LFS 文件
            content_preview = b""
            content_chunks = []

            for chunk in response.iter_content(chunk_size=self.config.chunk_size):
                if chunk:
                    content_chunks.append(chunk)
                    if len(content_preview) < 1024:  # 只读取前1KB用于检测
                        content_preview += chunk

                    # 如果已经读取了足够的内容用于检测，停止预览
                    if len(content_preview) >= 1024:
                        break

            # 检查是否为 Git LFS 文件
            content_preview_str = content_preview.decode("utf-8", errors="ignore")
            if self._is_lfs_pointer(content_preview_str):
                logger.info(f"检测到 LFS 文件: {file_path}")

                # 解析 LFS 信息
                lfs_info = self._parse_lfs_pointer(content_preview_str)
                if lfs_info:
                    # 下载 LFS 实际文件
                    success, error = self._download_lfs_file(lfs_info, local_path)
                    if success:
                        file_size = local_path.stat().st_size
                        self.stats.downloaded_files += 1
                        self.stats.downloaded_size += file_size
                        return True, str(local_path), None
                    else:
                        return False, file_path, f"LFS文件下载失败: {error}"
                else:
                    return False, file_path, "无法解析LFS指针文件"

            else:
                # 普通文件，直接保存
                with open(local_path, "wb") as f:
                    for chunk in content_chunks:
                        f.write(chunk)

                    # 继续读取剩余内容
                    for chunk in response.iter_content(
                        chunk_size=self.config.chunk_size
                    ):
                        if chunk:
                            f.write(chunk)

                file_size = local_path.stat().st_size
                self.stats.downloaded_files += 1
                self.stats.downloaded_size += file_size
                return True, str(local_path), None

        except Exception as e:
            error_msg = f"下载失败: {str(e)}"
            logger.error(f"下载文件 {file_path} 失败: {error_msg}")
            self.stats.failed_files += 1
            return False, file_path, error_msg

    def _is_lfs_pointer(self, content: str) -> bool:
        """
        检查内容是否为 Git LFS 指针文件

        Args:
            content: 文件内容

        Returns:
            是否为 LFS 指针文件
        """
        lines = content.strip().split("\n")
        if len(lines) >= 3:
            return (
                lines[0].startswith("version https://git-lfs.github.com/spec/v1")
                and any(line.startswith("oid sha256:") for line in lines)
                and any(line.startswith("size ") for line in lines)
            )
        return False

    def _parse_lfs_pointer(self, content: str) -> Optional[Dict]:
        """
        解析 Git LFS 指针文件

        Args:
            content: LFS 指针文件内容

        Returns:
            LFS 文件信息 {"oid": str, "size": int}
        """
        try:
            lines = content.strip().split("\n")
            oid = None
            size = None

            for line in lines:
                if line.startswith("oid sha256:"):
                    oid = line.split(":", 1)[1].strip()
                elif line.startswith("size "):
                    size = int(line.split(" ", 1)[1].strip())

            if oid and size:
                return {"oid": oid, "size": size}

        except Exception as e:
            logger.error(f"解析LFS指针失败: {e}")

        return None

    def _download_lfs_file(
        self, lfs_info: Dict, local_path: Path
    ) -> Tuple[bool, Optional[str]]:
        """
        从 Git LFS 服务器下载实际文件

        Args:
            lfs_info: LFS文件信息 {"oid": str, "size": int}
            local_path: 本地保存路径

        Returns:
            (是否成功, 错误信息)
        """
        try:
            oid = lfs_info["oid"]
            expected_size = lfs_info["size"]

            # 构建 LFS 下载 URL
            # GitLab LFS URL 格式: https://git:<access_token>@git.wanfeng-inc.com/<project_name>.git/gitlab-lfs/objects/<oid>
            domain_name = urlparse(self.config.gitlab_host).netloc
            lfs_url = f"https://git:{self.config.access_token}@{domain_name}/{self.config.project_path}.git/gitlab-lfs/objects/{oid}"
            logger.info(f"从 LFS 服务器下载: {lfs_url}")

            # 下载 LFS 文件
            response = self._request_with_retry("GET", lfs_url, stream=True)

            # 保存文件并验证
            downloaded_size = 0
            sha256_hash = hashlib.sha256()

            with open(local_path, "wb") as f:
                for chunk in response.iter_content(chunk_size=self.config.chunk_size):
                    if chunk:
                        f.write(chunk)
                        downloaded_size += len(chunk)
                        sha256_hash.update(chunk)

            # 验证文件完整性
            if downloaded_size != expected_size:
                return (
                    False,
                    f"文件大小不匹配: 期望 {expected_size}, 实际 {downloaded_size}",
                )

            actual_oid = sha256_hash.hexdigest()
            if actual_oid != oid:
                return False, f"文件SHA256不匹配: 期望 {oid}, 实际 {actual_oid}"

            logger.info(f"LFS文件下载成功: {local_path} ({downloaded_size} bytes)")
            return True, None

        except Exception as e:
            error_msg = f"LFS文件下载失败: {str(e)}"
            logger.error(error_msg)
            return False, error_msg

    def download_files(
        self,
        output_dir: str,
        branch: str = "main",
        download_config: Optional[DownloadConfig] = None,
        progress_callback: Optional[Callable] = None,
    ) -> DownloadResult:
        """
        智能下载仓库文件，自动选择最优策略

        Args:
            output_dir: 输出目录
            branch: 分支名
            download_config: 下载配置
            progress_callback: 进度回调函数

        Returns:
            下载结果
        """
        if download_config is None:
            download_config = DownloadConfig()

        # 预编译正则表达式以提高性能
        self._compile_patterns(download_config)

        # 智能选择下载策略
        strategy = self._estimate_download_strategy(download_config)
        logger.info(f"选择下载策略: {strategy.value}")

        if strategy == DownloadStrategy.GIT_CLONE:
            return self.download_files_with_git_clone(
                output_dir, branch, download_config, progress_callback
            )
        elif strategy == DownloadStrategy.GITLAB_API:
            return self._download_files_api(
                output_dir, branch, download_config, progress_callback
            )
        else:
            return DownloadResult(
                success=False,
                error=f"不支持的下载策略: {strategy.value}. ",
                stats=None,
            )

    def _download_files_api(
        self,
        output_dir: str,
        branch: str = "main",
        download_config: Optional[DownloadConfig] = None,
        progress_callback: Optional[Callable] = None,
    ) -> DownloadResult:
        """
        使用 API 方式下载文件（优化版本）

        Args:
            output_dir: 输出目录
            branch: 分支名
            download_config: 下载配置
            progress_callback: 进度回调函数

        Returns:
            下载结果
        """
        if download_config is None:
            download_config = DownloadConfig()

        # 重置统计信息
        self.stats = DownloadStats()
        self.stats.start_time = time.time()

        try:
            logger.info(f"开始 API 下载仓库文件: {self.config.repo_url}")

            # 1. 获取优化的文件树
            logger.info("步骤 1/3: 获取并过滤文件树...")
            target_files = self.get_repository_tree(branch, download_config)

            if not target_files:
                return DownloadResult(
                    success=True,
                    message="没有找到符合条件的文件",
                    stats=self.stats,
                )

            self.stats.filtered_files = len(target_files)
            logger.info(f"找到 {len(target_files)} 个目标文件")

            # 2. 并发下载
            logger.info("步骤 2/3: 开始并发下载...")
            os.makedirs(output_dir, exist_ok=True)

            successful_files = []
            failed_files = []

            with ThreadPoolExecutor(max_workers=self.config.max_workers) as executor:
                # 提交所有下载任务
                future_to_file = {
                    executor.submit(
                        self._download_single_file,
                        file_info,
                        output_dir,
                        branch,
                        download_config,
                    ): file_info
                    for file_info in target_files
                }

                # 处理完成的任务
                completed_count = 0
                for future in as_completed(future_to_file):
                    file_info = future_to_file[future]
                    file_path = file_info.get("path", "")

                    try:
                        success, local_path, error = future.result()
                        completed_count += 1

                        if success:
                            successful_files.append(
                                FileResult(
                                    file_path=file_path,
                                    local_path=local_path,
                                    size=file_info.get("size", 0),
                                    success=True,
                                )
                            )
                        else:
                            failed_files.append(
                                FileResult(
                                    file_path=file_path, error=error, success=False
                                )
                            )

                        # 调用进度回调
                        if progress_callback:
                            progress = (completed_count / len(target_files)) * 100
                            progress_callback(
                                completed_count, len(target_files), progress
                            )

                        # 输出进度日志
                        if completed_count % 10 == 0 or completed_count == len(
                            target_files
                        ):
                            logger.info(
                                f"下载进度: {completed_count}/{len(target_files)} "
                                f"({(completed_count/len(target_files)*100):.1f}%)"
                            )

                    except Exception as e:
                        logger.error(f"处理文件 {file_path} 时出错: {e}")
                        failed_files.append(
                            FileResult(file_path=file_path, error=str(e), success=False)
                        )
                        self.stats.failed_files += 1

            self.stats.end_time = time.time()

            logger.info(
                f"API 下载完成: 成功 {self.stats.downloaded_files} 个, "
                f"失败 {self.stats.failed_files} 个, "
                f"跳过 {self.stats.skipped_files} 个, "
                f"耗时 {self.stats.duration:.2f} 秒, "
                f"平均速度 {self.stats.average_speed:.2f} MB/s"
            )

            return DownloadResult(
                success=True,
                stats=self.stats,
                successful_files=successful_files,
                failed_files=failed_files,
                output_dir=output_dir,
            )

        except Exception as e:
            self.stats.end_time = time.time()
            logger.error(f"API 下载文件时出错: {e}")
            return DownloadResult(success=False, error=str(e), stats=self.stats)

    def clone_repository(
        self, local_path: str, branch: str = "main", depth: int = 1
    ) -> CloneResult:
        """
        克隆仓库到本地

        Args:
            local_path: 本地路径
            branch: 分支名
            depth: 克隆深度

        Returns:
            克隆结果
        """
        try:
            logger.info(f"开始克隆仓库: {self.config.repo_url}, 分支: {branch}")

            # 创建目录
            repo_dir = Path(local_path)
            repo_dir.mkdir(parents=True, exist_ok=True)

            # 清理已存在的内容
            if repo_dir.exists() and any(repo_dir.iterdir()):
                logger.warning(f"目录已存在，正在清理: {repo_dir}")
                shutil.rmtree(repo_dir)
                repo_dir.mkdir(parents=True, exist_ok=True)

            # 构建Git命令
            clone_url = self.config.repo_url
            if self.config.access_token:
                clone_url = f"{self.config.gitlab_host}/oauth2:{self.config.access_token}@{self.config.gitlab_host.split('://')[-1]}/{self.config.project_path}.git"

            cmd = [
                "git",
                "clone",
                "--branch",
                branch,
                "--single-branch",
                "--depth",
                str(depth),
                clone_url,
                str(repo_dir),
            ]

            if self.config.skip_ssl_verify:
                cmd.insert(2, "-c")
                cmd.insert(3, "http.sslVerify=false")

            # 执行克隆
            logger.info(
                f"执行克隆命令: git clone --branch {branch} --single-branch --depth {depth} [URL] {repo_dir}"
            )

            process = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=300,
                text=True,
            )

            if process.returncode != 0:
                return CloneResult(
                    success=False, error=f"Git克隆失败: {process.stderr}"
                )

            logger.info("仓库克隆成功")
            return CloneResult(success=True, path=str(repo_dir), message="仓库克隆成功")

        except Exception as e:
            logger.error(f"克隆仓库失败: {e}")
            return CloneResult(success=False, error=str(e))

    def cleanup_old_repositories(self, base_dir: str, days: int = 7):
        """
        清理旧的仓库目录

        Args:
            base_dir: 基础目录
            days: 保留天数
        """
        try:
            logger.info(f"开始清理超过 {days} 天的旧仓库")

            now = time.time()
            cutoff_time = now - (days * 24 * 60 * 60)

            base_path = Path(base_dir)
            if not base_path.exists():
                return

            removed_count = 0
            for item in base_path.iterdir():
                if item.is_dir():
                    # 检查目录的修改时间
                    if item.stat().st_mtime < cutoff_time:
                        try:
                            shutil.rmtree(item)
                            removed_count += 1
                            logger.info(f"已删除旧目录: {item}")
                        except Exception as e:
                            logger.error(f"删除目录失败 {item}: {e}")

            logger.info(f"清理完成，共删除 {removed_count} 个目录")

        except Exception as e:
            logger.error(f"清理旧仓库失败: {e}")

    def get_download_stats(self) -> DownloadStats:
        """获取下载统计信息"""
        return self.stats

    def _compile_patterns(self, download_config: DownloadConfig):
        """
        预编译正则表达式模式以提高性能

        Args:
            download_config: 下载配置
        """
        self._compiled_patterns = {
            "include": [
                re.compile(pattern) for pattern in download_config.include_patterns
            ],
            "exclude": [
                re.compile(pattern) for pattern in download_config.exclude_patterns
            ],
        }

        # 如果配置中包含通配符 *，则允许所有扩展名
        self._allow_all_extensions = (
            "*" in download_config.file_extensions
            if download_config.file_extensions
            else False
        )

        # 缓存文件扩展名集合
        self._allowed_extensions = (
            set(ext.lower() for ext in download_config.file_extensions)
            if download_config.file_extensions
            else set()
        )

    def get_repository_info(self) -> RepositoryInfo:
        """
        获取仓库基本信息，用于决定下载策略

        Returns:
            仓库信息
        """
        try:
            api_url = self._get_api_url(self.config.project_path)
            response = self._request_with_retry("GET", api_url)
            repo_info = response.json()

            # 获取仓库统计信息
            stats_url = self._get_api_url(
                self.config.project_path, "repository/contributors"
            )
            try:
                stats_response = self._request_with_retry(
                    "GET", stats_url, params={"per_page": 1}
                )
                # 尝试获取更多统计信息
            except:
                pass

            return RepositoryInfo(
                size=repo_info.get("repository_size", 0),
                default_branch=repo_info.get("default_branch", "main"),
                file_count=repo_info.get("repository_file_count", 0),
                last_activity=repo_info.get("last_activity_at"),
                web_url=repo_info.get("web_url"),
            )
        except Exception as e:
            logger.warning(f"获取仓库信息失败: {e}")
            return RepositoryInfo()

    def _estimate_download_strategy(
        self, download_config: DownloadConfig
    ) -> DownloadStrategy:
        """
        根据仓库信息和下载配置智能选择下载策略

        Args:
            download_config: 下载配置

        Returns:
            推荐的下载策略
        """
        repo_info = self.get_repository_info()
        repo_size = repo_info.size  # 字节
        file_count = repo_info.file_count

        # 如果用户指定了策略，优先使用
        if download_config.strategy != DownloadStrategy.AUTO:
            return download_config.strategy

        # 策略决策逻辑
        # 1. 如果仓库很小（< 100MB）或文件很少（< 1000个），使用 API
        if repo_size < 100 * 1024 * 1024 or file_count < 1000:
            logger.info("仓库较小，使用 API 选择性下载")
            return DownloadStrategy.GITLAB_API

        # 2. 如果需要下载所有文件（通配符 *），使用 git clone
        if self._allow_all_extensions or not download_config.file_extensions:
            logger.info("需要下载所有文件，使用 Git Clone")
            return DownloadStrategy.GIT_CLONE

        # 3. 默认使用 API
        return DownloadStrategy.GITLAB_API

    def get_repository_tree(
        self,
        branch: str = "main",
        download_config: Optional[DownloadConfig] = None,
        path: str = "",
        max_depth: int = 0,
    ) -> List[Dict]:
        """
        优化的文件树获取，支持按需获取和智能过滤

        Args:
            branch: 分支名
            download_config: 下载配置，用于智能过滤
            path: 起始路径
            max_depth: 最大深度，0表示不限制深度

        Returns:
            过滤后的文件列表
        """
        try:
            target_files = []

            # 获取完整文件树，使用分批处理和早期过滤
            target_files = self._get_filtered_tree(
                branch, download_config, path, max_depth
            )

            self.stats.total_files = len(target_files)
            logger.info(f"优化获取完成，找到 {len(target_files)} 个目标文件")
            return target_files

        except Exception as e:
            logger.error(f"获取优化文件树失败: {e}")
            return []

    def _get_filtered_tree(
        self,
        branch: str,
        download_config: DownloadConfig,
        path: str = "",
        max_depth: int = 0,
    ) -> List[Dict]:
        """
        获取过滤后的文件树，使用并发请求和边获取边过滤优化

        Args:
            branch: 分支名
            download_config: 下载配置
            path: 路径
            max_depth: 最大深度，0表示不限制深度

        Returns:
            过滤后的文件列表
        """
        target_files = []

        try:
            # 1. 首先获取总页数
            total_pages = self._get_total_pages(branch, path)
            if total_pages == 0:
                logger.info("没有找到任何文件")
                return []

            logger.info(f"总共需要请求 {total_pages} 页数据")

            # 2. 使用线程池并发请求所有页面
            with ThreadPoolExecutor(max_workers=self.config.max_workers) as executor:
                # 提交所有页面的请求任务
                future_to_page = {
                    executor.submit(self._fetch_tree_page, branch, path, page): page
                    for page in range(1, total_pages + 1)
                }

                # 处理完成的任务
                processed_pages = 0
                for future in as_completed(future_to_page):
                    page_num = future_to_page[future]

                    try:
                        files = future.result()
                        if files:
                            # 边获取边过滤，减少内存占用
                            for file_info in files:
                                if self._should_download_file(
                                    file_info, download_config
                                ):
                                    target_files.append(file_info)

                        processed_pages += 1

                        # 输出进度日志
                        if processed_pages % 10 == 0 or processed_pages == total_pages:
                            logger.info(
                                f"树获取进度: {processed_pages}/{total_pages} 页, "
                                f"当前匹配文件数: {len(target_files)}"
                            )

                    except Exception as e:
                        logger.error(f"处理第 {page_num} 页时出错: {e}")
                        continue

        except Exception as e:
            logger.error(f"获取过滤文件树失败: {e}")

        return target_files

    def _get_total_pages(self, branch: str, path: str = "") -> int:
        """
        获取总页数

        Args:
            branch: 分支名
            path: 路径

        Returns:
            总页数
        """
        try:
            api_url = self._get_api_url(self.config.project_path, "repository/tree")
            params = {
                "ref": branch,
                "path": path,
                "recursive": True,
                "per_page": self.config.per_page,
                "page": 1,
            }

            response = self._request_with_retry("GET", api_url, params=params)

            # 从响应头获取总页数
            total_pages_str = response.headers.get("X-Total-Pages", "0")
            total_pages = int(total_pages_str)

            # 如果没有总页数信息，通过第一页判断
            if total_pages == 0:
                files = response.json()
                if len(files) < self.config.per_page:
                    total_pages = 1
                else:
                    # 如果第一页满了，可能还有更多页，这种情况下回退到原始逻辑
                    logger.warning("无法从响应头获取总页数，回退到逐页请求模式")
                    return 0

            logger.info(f"从响应头获取到总页数: {total_pages}")
            return total_pages

        except Exception as e:
            logger.error(f"获取总页数失败: {e}")
            return 0

    def _fetch_tree_page(self, branch: str, path: str, page: int) -> List[Dict]:
        """
        获取指定页的文件树数据

        Args:
            branch: 分支名
            path: 路径
            page: 页码

        Returns:
            文件列表
        """
        try:
            api_url = self._get_api_url(self.config.project_path, "repository/tree")
            params = {
                "ref": branch,
                "path": path,
                "recursive": True,
                "per_page": self.config.per_page,
                "page": page,
            }

            response = self._request_with_retry("GET", api_url, params=params)
            files = response.json()

            return files if isinstance(files, list) else []

        except Exception as e:
            logger.error(f"获取第 {page} 页文件树失败: {e}")
            return []

    def download_files_with_git_clone(
        self,
        repo_base_dir: str = ".",
        branch: str = "master",
        download_config: Optional[DownloadConfig] = None,
        progress_callback: Optional[Callable] = None,
    ) -> DownloadResult:
        """
        使用 Git Clone 方式下载文件（全量下载无过滤，支持增量更新）

        Args:
            output_dir: 输出目录
            branch: 分支名
            download_config: 下载配置
            progress_callback: 进度回调函数 (current_step, total_steps, progress_percent, message)

        Returns:
            下载结果
        """
        if download_config is None:
            download_config = DownloadConfig()

        self.stats = DownloadStats()
        self.stats.start_time = time.time()

        try:
            # 仓库保存目录位置（指定仓库名称为对应项目名）
            # repo_dir = Path(repo_base_dir) / self._generate_repo_key()
            repo_dir = Path(repo_base_dir).resolve()  # 转换为绝对路径
            logger.info(f"⭐下载策略 [Git Clone]: {self.config.repo_url} | branch: {branch} -> {repo_dir}")

            # 判断是否启用增量更新
            # 当目录存在且包含 .git 文件夹时，优先使用增量更新
            # 如果用户设置了 overwrite_existing=False，则跳过更新
            repo_exists = repo_dir.exists()
            git_dir_exists = (repo_dir / ".git").exists()
            is_git_repo = repo_exists and git_dir_exists
            
            logger.info(f"仓库目录检测: repo_dir={repo_dir} (绝对路径)")
            logger.info(f"目录存在: {repo_exists}, .git存在: {git_dir_exists}, 是Git仓库: {is_git_repo}")
            
            # 额外调试信息
            if repo_exists:
                logger.info(f"目录内容: {list(repo_dir.iterdir())[:5]}...")  # 显示前5个文件/目录
            if not git_dir_exists and repo_exists:
                logger.warning(f"目录存在但缺少.git目录，可能不是有效的Git仓库")

            if is_git_repo:
                # 增量更新模式：仓库存在且允许更新
                logger.info("> 检测到现有仓库，使用增量更新模式...")
                git_dir = self._update_repository_with_progress(
                    repo_dir, branch, progress_callback
                )
            else:
                # 全新克隆模式：仓库不存在或不是有效的 git 仓库
                logger.info("> 使用全新克隆模式...")
                git_dir = self._clone_repository_with_progress(
                    repo_dir, branch, progress_callback
                )

            self.stats.end_time = time.time()

            msg = (
                f"{'更新' if is_git_repo else '克隆'}仓库成功: {git_dir},"
                f"耗时 {self.stats.duration:.2f} 秒"
            )
            logger.info(msg)

            if progress_callback:
                progress_callback(4, 4, 100.0, "下载完成")

            return DownloadResult(
                success=True,
                stats=self.stats,
                successful_files=0,  # 未使用文件列表
                failed_files=0,
                output_dir=str(repo_dir),
                message=msg,
            )

        except Exception as e:
            self.stats.end_time = time.time()
            logger.error(f"Git Clone 下载失败: {e}")
            return DownloadResult(success=False, error=str(e), stats=self.stats)

    @staticmethod
    def _get_optimal_max_workers() -> int:
        """
        根据系统硬件自动计算最优的线程数

        Returns:
            推荐的线程数
        """
        try:
            import psutil

            # 获取 CPU 核心数
            cpu_count = psutil.cpu_count()  # 逻辑核心数
            physical_cpu_count = psutil.cpu_count(logical=False)  # 物理核心数

            # 获取可用内存 (GB)
            memory_gb = psutil.virtual_memory().available / (1024**3)

            # 对于 I/O 密集型任务（如网络请求），计算公式：
            # 基础线程数 = CPU 核心数 * 2-4 倍（考虑 I/O 等待）
            base_workers = cpu_count * 3

            # 内存限制：假设每个线程大约占用 5-10MB 内存
            memory_limited_workers = int(memory_gb * 100)  # 保守估计

            # 取较小值，并设置合理的上下限
            optimal_workers = min(base_workers, memory_limited_workers)
            optimal_workers = max(2, min(optimal_workers, 50))  # 最少2个，最多50个

            logger.info(
                f"自动检测系统配置: CPU核心={cpu_count}, 内存={memory_gb:.1f}GB"
            )
            logger.info(f"推荐线程数: {optimal_workers}")

            return optimal_workers

        except ImportError:
            # 如果没有 psutil，使用基本的 CPU 检测
            import os

            cpu_count = os.cpu_count() or 4
            optimal_workers = min(cpu_count * 2, 20)
            logger.warning("未安装 psutil，使用基础 CPU 检测")
            logger.info(f"CPU核心数: {cpu_count}, 推荐线程数: {optimal_workers}")
            return optimal_workers

        except Exception as e:
            logger.warning(f"自动检测线程数失败: {e}，使用默认值 10")
            return 10

    def _generate_repo_key(self) -> str:
        """
        生成仓库缓存目录的唯一标识

        Returns:
            缓存目录标识
        """
        # 基于 self.config.repo_url 生成唯一的缓存目录标识
        return hashlib.md5(self.config.repo_url.encode()).hexdigest()[:8]

    def _clone_repository_with_progress(
        self, repo_dir: Path, branch: str, progress_callback: Optional[Callable] = None
    ) -> str:
        """
        使用认证信息克隆仓库

        Args:
            repo_dir: 缓存目录
            branch: 分支名
            progress_callback: 进度回调函数

        Returns:
            克隆目录路径
        """
        try:
            if progress_callback:
                progress_callback(1, 4, 10.0, f"开始克隆仓库分支 {branch}...")

            # 避免无必要删除：若目录存在且为git仓库，则不在此函数中处理克隆
            if repo_dir.exists() and (repo_dir / ".git").exists():
                logger.warning(f"目标目录已是Git仓库，跳过删除与克隆: {repo_dir}")
                return str(repo_dir)

            # 确保克隆目标目录存在（为空目录）
            repo_dir.mkdir(parents=True, exist_ok=True)

            # 构建带认证的克隆URL
            if self.config.access_token:
                parsed = urlparse(self.config.repo_url)
                if parsed.scheme in ["http", "https"]:
                    clone_url = f"{parsed.scheme}://oauth2:{self.config.access_token}@{parsed.netloc}{parsed.path}"
                else:
                    clone_url = self.config.repo_url
            else:
                clone_url = self.config.repo_url

            # 构建 git clone 命令
            cmd = [
                "git",
                "clone",
                "--branch",
                branch,
                # "--single-branch",
                clone_url,
                str(repo_dir),
            ]

            # SSL 验证配置
            env = os.environ.copy()
            if self.config.skip_ssl_verify:
                env["GIT_SSL_NO_VERIFY"] = "1"

            logger.info(f"执行 Git Clone: 分支 {branch}")

            if progress_callback:
                progress_callback(2, 4, 25.0, "正在克隆仓库...")

            # 执行 git clone 命令
            process = subprocess.run(
                cmd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=3600,  # 设置1小时超时
                text=True,
            )

            if process.returncode != 0:
                raise Exception(f"Git Clone 失败: {process.stderr}")

            if progress_callback:
                progress_callback(3, 4, 75.0, "仓库克隆完成")

            logger.info("仓库克隆成功")
            return str(repo_dir)

        except Exception as e:
            logger.error(f"克隆仓库失败: {e}")
            raise

    def _update_repository_with_progress(
        self,
        repo_dir: Path,
        branch: str,
        progress_callback: Optional[Callable] = None,
    ) -> str:
        """
        使用 Git Pull 进行增量更新并监控进度

        Args:
            repo_dir: 缓存目录
            branch: 分支名
            progress_callback: 进度回调函数

        Returns:
            更新后的目录路径
        """
        try:
            if progress_callback:
                progress_callback(1, 4, 10.0, f"检查仓库状态...")

            # 验证是否为有效的 git 仓库
            if not (repo_dir / ".git").exists():
                logger.warning(f"缓存目录 {repo_dir} 不是有效的 git 仓库，执行全新克隆")
                return self._clone_repository_with_progress(
                    repo_dir, branch, progress_callback
                )

            # 构建带认证的远程 URL（用于可能的认证更新）
            if self.config.access_token:
                parsed = urlparse(self.config.repo_url)
                if parsed.scheme in ["http", "https"]:
                    remote_url = f"{parsed.scheme}://oauth2:{self.config.access_token}@{parsed.netloc}{parsed.path}"
                else:
                    remote_url = self.config.repo_url
            else:
                remote_url = self.config.repo_url

            # 设置环境变量
            env = os.environ.copy()
            if self.config.skip_ssl_verify:
                env["GIT_SSL_NO_VERIFY"] = "1"

            if progress_callback:
                progress_callback(1, 4, 20.0, f"更新远程 URL...")

            # 更新远程 URL（防止认证信息变化）
            subprocess.run(
                ["git", "remote", "set-url", "origin", remote_url],
                cwd=repo_dir,
                check=True,
                env=env,
                capture_output=True,
            )

            if progress_callback:
                progress_callback(1, 4, 30.0, f"获取远程分支信息...")

            # 首先获取远程分支的最新信息
            try:
                subprocess.run(
                    ["git", "fetch", "origin"],
                    cwd=repo_dir,
                    check=True,
                    env=env,
                    capture_output=True,
                )
                logger.info("成功获取远程分支信息")
            except subprocess.CalledProcessError as e:
                logger.warning(f"获取远程分支信息失败: {e}")

            if progress_callback:
                progress_callback(1, 4, 35.0, f"切换到分支 {branch}...")

            # 切换到目标分支
            try:
                # 首先检查远程分支是否存在
                try:
                    subprocess.run(
                        [
                            "git",
                            "show-ref",
                            "--verify",
                            "--quiet",
                            f"refs/remotes/origin/{branch}",
                        ],
                        cwd=repo_dir,
                        check=True,
                        env=env,
                        capture_output=True,
                    )
                    remote_branch_exists = True
                except subprocess.CalledProcessError:
                    remote_branch_exists = False
                    logger.warning(f"远程分支 origin/{branch} 不存在")

                # 检查本地分支是否存在
                try:
                    subprocess.run(
                        [
                            "git",
                            "show-ref",
                            "--verify",
                            "--quiet",
                            f"refs/heads/{branch}",
                        ],
                        cwd=repo_dir,
                        check=True,
                        env=env,
                        capture_output=True,
                    )
                    local_branch_exists = True
                except subprocess.CalledProcessError:
                    local_branch_exists = False

                if local_branch_exists:
                    # 本地分支存在，直接切换
                    subprocess.run(
                        ["git", "checkout", branch],
                        cwd=repo_dir,
                        check=True,
                        env=env,
                        capture_output=True,
                    )
                    logger.info(f"切换到本地分支: {branch}")
                elif remote_branch_exists:
                    # 本地分支不存在但远程分支存在，创建并跟踪远程分支
                    subprocess.run(
                        ["git", "checkout", "-b", branch, f"origin/{branch}"],
                        cwd=repo_dir,
                        check=True,
                        env=env,
                        capture_output=True,
                    )
                    logger.info(f"创建并切换到分支: {branch} (跟踪 origin/{branch})")
                else:
                    # 本地和远程分支都不存在
                    raise Exception(f"分支 {branch} 在本地和远程仓库中都不存在")

            except subprocess.CalledProcessError as e:
                logger.error(f"分支切换失败: {e}")
                raise Exception(f"无法切换到分支 {branch}: {e}")

            if progress_callback:
                progress_callback(2, 4, 40.0, f"拉取最新更改...")

            # 执行 git pull 更新到分支最新版本
            # 使用 --ff-only 确保只进行快进合并，避免意外的合并提交
            pull_cmd = ["git", "pull", "--ff-only", "origin", branch]

            logger.info(f"执行 git pull 更新分支 {branch} 到最新版本")

            # 直接执行 git pull 命令
            process = subprocess.run(
                pull_cmd,
                cwd=repo_dir,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=1800,  # 30分钟超时
            )

            if process.returncode != 0:
                error_msg = process.stderr.strip() if process.stderr else "未知错误"
                if "non-fast-forward" in error_msg.lower():
                    raise Exception(
                        f"Git Pull 失败: 检测到非快进更新，可能存在冲突。建议检查本地修改。错误信息: {error_msg}"
                    )
                else:
                    raise Exception(f"Git Pull 失败: {error_msg}")

            # 记录成功信息
            if process.stdout:
                success_output = process.stdout.strip()
                if success_output:
                    logger.info(f"Git Pull 成功: {success_output}")

            if progress_callback:
                progress_callback(3, 4, 60.0, "更新完成")

            # 验证当前分支和远程分支是否同步
            try:
                # 获取当前分支的 commit hash
                result = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=repo_dir,
                    check=True,
                    env=env,
                    capture_output=True,
                    text=True,
                )
                local_commit = result.stdout.strip()

                # 获取远程分支的 commit hash
                result = subprocess.run(
                    ["git", "rev-parse", f"origin/{branch}"],
                    cwd=repo_dir,
                    check=True,
                    env=env,
                    capture_output=True,
                    text=True,
                )
                remote_commit = result.stdout.strip()

                if local_commit == remote_commit:
                    logger.info(
                        f"分支 {branch} 已成功更新到最新版本 ({local_commit[:8]})"
                    )
                else:
                    logger.warning(
                        f"分支同步可能存在问题: 本地 {local_commit[:8]}, 远程 {remote_commit[:8]}"
                    )

            except subprocess.CalledProcessError as e:
                logger.warning(f"验证分支同步状态失败: {e}")

            if progress_callback:
                progress_callback(3, 4, 80.0, "增量更新完成")

            logger.info(f"仓库增量更新成功: {repo_dir}")
            return str(repo_dir)

        except Exception as e:
            # 非必要禁止删除：增量更新失败时不回退到全新克隆，保留现有仓库
            logger.error(f"增量更新失败: {e}，已保留现有仓库: {repo_dir}")
            return str(repo_dir)

    def get_repo_name(self, repo_url: str) -> str:
        """
        从仓库URL中提取仓库名称

        Args:
            repo_url: 仓库URL

        Returns:
            仓库名称
        """
        try:
            project_host, project_path = self.parse_gitlab_url(repo_url)
            repo_name = project_path.split('/')[-1]
            return repo_name
        except Exception as e:
            logger.error(f"从仓库URL提取名称失败: {e}")
            return ""

    def get_repository_directories(
        self, branch: str = "main", path: str = ""
    ) -> List[Dict]:
        """
        获取指定路径下的所有目录（非递归）

        Args:
            branch: 分支名
            path: 路径

        Returns:
            目录列表 [{"id": "...", "name": "...", "type": "tree", "path": "...", "mode": "..."}]
        """
        directories = []
        page = 1

        try:
            while True:
                api_url = self._get_api_url(self.config.project_path, "repository/tree")
                params = {
                    "ref": branch,
                    "path": path,
                    "recursive": False,  # 不递归，只获取当前层级
                    "per_page": self.config.per_page,
                    "page": page,
                }

                response = self._request_with_retry("GET", api_url, params=params)
                items = response.json()

                if not items or not isinstance(items, list):
                    break

                # 筛选目录类型
                for item in items:
                    if item.get("type") == "tree":
                        directories.append(item)

                # 检查是否有下一页
                total_pages = int(response.headers.get("X-Total-Pages", "0"))
                if total_pages > 0:
                    if page >= total_pages:
                        break
                else:
                    # 如果没有 total_pages 头，检查当前页是否满
                    if len(items) < self.config.per_page:
                        break

                page += 1

            return directories

        except Exception as e:
            logger.error(f"获取目录列表失败: {e}")
            return []

# 便捷函数和工厂方法
def create_gitlab_service(
    repo_url: str, token: Optional[str] = None, **kwargs
) -> GitLabService:
    """
    创建GitLab服务实例的便捷函数

    Args:
        repo_url: 仓库URL
        access_token: 访问令牌
        **kwargs: 其他配置参数

    Returns:
        GitLab服务实例
    """
    config = GitLabConfig(repo_url=repo_url, access_token=token, **kwargs)
    return GitLabService(config)


def download_repository_files(
    repo_url: str,
    output_dir: str,
    access_token: Optional[str] = None,
    branch: str = "main",
    file_extensions: Optional[List[str]] = None,
    **kwargs,
) -> Dict:
    """
    下载仓库文件的便捷函数

    Args:
        repo_url: 仓库URL
        output_dir: 输出目录
        access_token: 访问令牌
        branch: 分支名
        file_extensions: 文件扩展名列表
        **kwargs: 其他配置参数

    Returns:
        下载结果
    """
    # 创建服务实例
    service = create_gitlab_service(repo_url, access_token, **kwargs)

    # 创建下载配置
    download_config = DownloadConfig()
    if file_extensions:
        download_config.file_extensions = file_extensions

    return service.download_files(output_dir, branch, download_config)


# 使用示例
if __name__ == "__main__":

    def progress_callback(current_step, total_steps, percentage, message):
        """
        进度回调函数示例 - 适用于优化后的 Git Clone 策略

        Args:
            current_step: 当前步骤 (1-4)
            total_steps: 总步骤数 (固定为4)
            percentage: 进度百分比 (0-100)
            message: 当前状态信息
        """
        print(f"步骤 {current_step}/{total_steps} ({percentage:.1f}%): {message}")

    # 创建优化配置
    config = GitLabConfig(
        repo_url="https://git.wanfeng-inc.com/CardGame2.0/CardGame2.git",
        access_token="glpat-SJe3ZHya3fHS5wYUtgwz",
    )

    # 创建服务实例
    service = GitLabService(config)

    # 验证仓库
    if service.validate_repository():
        print("仓库验证成功")

        # 获取仓库信息用于决策
        repo_info = service.get_repository_info()
        print(f"仓库信息: {repo_info}")

        # 获取分支列表
        branches = service.get_repository_branches()
        print(f"可用分支: {branches}")

        # 优化下载配置示例

        # 示例1: Git Clone 策略 - 全量下载，支持增量更新
        download_config = DownloadConfig(
            strategy=DownloadStrategy.GIT_CLONE,  # 强制使用 Git Clone 策略
        )

        print("\n=== 使用优化的 Git Clone 策略下载（支持增量更新） ===")
        result: DownloadResult = service.download_files_with_git_clone(
            repo_base_dir="./git_cache",
            branch="master",
            download_config=download_config,
            progress_callback=progress_callback,
        )

        if result.success:
            stats = result.stats
            print(f"\n=== 下载完成 ===")
            print(f"策略: Git Clone")
            print(f"缓存信息: {result.message}")
            print(
                f"成功: {stats.downloaded_files}, 失败: {stats.failed_files}, 跳过: {stats.skipped_files}"
            )
            print(f"总大小: {stats.downloaded_size / 1024 / 1024:.2f} MB")
            print(f"耗时: {stats.duration:.2f}秒")
            print(f"成功率: {stats.success_rate:.1f}%")

        else:
            print(f"下载失败: {result.error}")

    else:
        print("仓库验证失败")

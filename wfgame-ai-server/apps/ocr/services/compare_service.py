import os
import logging

from typing import List, Dict, Optional
from pydantic import BaseModel, PrivateAttr
from django.conf import settings

from .gitlab import GitLabConfig, GitLabService, DownloadResult

logger = logging.getLogger(__name__)

class RepoMapping(BaseModel):
    """
    译文仓库子目录与原始资源目录映射关系
    """
    trans_subdir: str  # 翻译仓库子目录（相对于 target_path）
    source_subdir: str  # 原始仓库子目录（相对于原始图片下载的文件夹根目录）

class TransRepoConfig(BaseModel):
    url: str
    branch: str
    access_token: str
    target_dir: str
    target_path: str  # 项目代码所在目录，与 target_dir 相对路径
    mapping: List[RepoMapping]

    # 业务私有属性
    _repo_index: Dict[str, set] = PrivateAttr(default=None)

    def repo_path(self) -> str:
        """
        获取完整的本地仓库路径
        """
        return os.path.join(self.target_dir, self.target_path)


    def download_repo(self) -> DownloadResult:
        """
        根据当前的配置信息，下载 Git 仓库到指定目录
        """
        git_config = GitLabConfig(
            repo_url=self.url,
            access_token=self.access_token,
        )
        git_service = GitLabService(config=git_config)
        result: DownloadResult = git_service.download_files_with_git_clone(
            repo_base_dir=self.repo_path(),
            branch=self.branch,
        )
        return result

    def build_repo_index(self) -> Dict[str, set]:
        """
        为每个映射的翻译子目录建立文件索引

        Returns:
            Dict[str, set]: key 为 trans_subdir，value 为该目录下所有文件的相对路径集合
        """
        repo_index = {}
        base_repo_path = self.repo_path()
        for mapping in self.mapping:
            trans_dir_path = os.path.join(base_repo_path, mapping.trans_subdir)
            file_set = set()
            if not os.path.exists(trans_dir_path):
                logger.warning(f"索引目录不存在，已跳过: {trans_dir_path}")
                repo_index[mapping.trans_subdir] = file_set
                continue

            logger.info(f"正在建立索引: {trans_dir_path} ...")
            # os.walk 可能会因权限问题等抛出异常，让它自然抛出
            for root, _, files in os.walk(trans_dir_path):
                for file in files:
                    # 获取全路径
                    full_path = os.path.join(root, file)
                    # 获取相对于 trans_dir_path 的路径
                    rel_path = os.path.relpath(full_path, trans_dir_path)
                    # 统一转为 / 分隔符，方便后续匹配
                    rel_path = rel_path.replace('\\', '/')
                    file_set.add(rel_path)
            repo_index[mapping.trans_subdir] = file_set

        self._repo_index = repo_index
        return self._repo_index


    def match_image_path(self, image_path: str) -> Optional[str]:
        """
        检查给定的 image_path 是否在翻译仓库中有对应的译文文件

        Args:
            image_path (str): 原始图片路径

        Returns:
            Optional[str]: 如果找到对应的译文文件，返回其相对 media 的相对路径（以入库方便前端请求图片）
        """
        if self._repo_index is None:
            raise ValueError("请先调用 build_repo_index() 方法建立索引")

        if not image_path:
            return None

        for mapping in self.mapping:
            rel_path = get_relative_path_from_anchors(image_path, [mapping.source_subdir])
            if rel_path is None:
                continue  # 该映射子目录未匹配到锚点，跳过
            if rel_path in self._repo_index.get(mapping.trans_subdir, set()):
                # 找到匹配的译文文件
                full_path = os.path.join(
                    self.repo_path(),
                    mapping.trans_subdir,
                    rel_path
                )
                # 计算相对于 media 根目录的路径
                return os.path.relpath(full_path, settings.MEDIA_ROOT).replace('\\', '/')
        return None

    def locate_trans_image_path(self, image_path: str) -> Optional[str]:
        """
        [非索引方案] 利用 os.path.exists
        尝试根据 image_path 定位翻译仓库中的对应文件，如果存在则返回相对 media 的路径
        Args:
            image_path (str): 原始图片路径
        Returns:
            Optional[str]: 如果找到对应的译文文件，返回其相对 media 的相对路径（以入库方便前端请求图片）
        """
        if not image_path:
            return None

        for mapping in self.mapping:
            rel_path = get_relative_path_from_anchors(image_path, [mapping.source_subdir])
            if rel_path is None:
                continue  # 该映射子目录未匹配到锚点，跳过

            full_path = os.path.join(
                self.repo_path(),
                mapping.trans_subdir,
                rel_path
            )
            if os.path.exists(full_path):
                # 找到匹配的译文文件
                return os.path.relpath(full_path, settings.MEDIA_ROOT).replace('\\', '/')
        return None


def get_relative_path_from_anchors(image_path: str, anchors: List[str]) -> Optional[str]:
    """
    根据锚点列表，从 image_path 中提取相对路径
    例如: image_path=".../Client/assets/A/B.png", anchor="assets" -> 返回 "A/B.png"
    """
    # 统一路径分隔符
    path_str = image_path.replace('\\', '/')

    for anchor in anchors:
        # 构造锚点特征，确保匹配的是目录名
        # 1. 尝试匹配 "/anchor/"
        token = f"/{anchor}/"
        if token in path_str:
            return path_str.split(token, 1)[1]

        # 2. 尝试匹配以 "anchor/" 开头的情况
        start_token = f"{anchor}/"
        if path_str.startswith(start_token):
            return path_str[len(start_token):]

    return None

trans_repo = TransRepoConfig(
    url="https://git.wanfeng-inc.com/CardGame2.0/cardgame2-language.git",
    branch="korea",
    access_token="glpat-SJe3ZHya3fHS5wYUtgwz",
    target_dir="PathUtils.get_ocr_repos_dir()",
    target_path="CardGame2",
    mapping=[
        RepoMapping(
            trans_subdir="assetsTransBack",
            source_subdir="Client/assets"
        )
    ]
)

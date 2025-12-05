"""
OCR异步任务处理
基于Celery实现的异步OCR任务
"""

import os
import logging
from pathlib import Path
from django.utils import timezone
from django.conf import settings
from celery import shared_task
import traceback
import datetime
import time
import json
import base64
import io
from PIL import Image
from django.template.loader import render_to_string

from .models import OCRTask, OCRResult, OCRGitRepository, OCRCache, OCRCacheHit
from apps.ocr.services.ocr_service import OCRService
from apps.ocr.services.multi_thread_ocr import MultiThreadOCR
from apps.ocr.services.two_stage_ocr import TwoStageOCRService
from apps.ocr.services.performance_config import get_performance_config
from .services.gitlab import (
    DownloadResult,
    GitLabService,
    GitLabConfig,
)
from .services.path_utils import PathUtils
from apps.notifications.tasks import notify_ocr_task_progress
from .services.compare_service import TransRepoConfig
from .serializers import OCRTaskSerializer, OCRResultSerializer
from enum import Enum

class BaseEnum(Enum):
    def __new__(cls, value, label, order, type=None, color=None):
        obj = object.__new__(cls)
        obj._value_ = value
        obj.label = label
        obj.order = order
        obj.type = type
        obj.color = color
        return obj

class ocrResultTypeEnum(BaseEnum):
    ALL = ("", "全部", 0, None, "#FFF")
    RIGHT = (1, "正确", 2, "success", "#90e9a6ff")
    WRONG = (2, "错误", 3, "danger", "#faa7a7ff")

class ocrIsVerifiedEnum(BaseEnum):
    ALL = (None, "全部", 1)
    VERIFIED = (True, "已审核", 2)
    UNVERIFIED = (False, "待审核", 3)

class ocrIsTranslatedEnum(BaseEnum):
    ALL = (None, "全部", 1)
    TRANSLATED = (True, "已翻译", 2)
    UNTRANSLATED = (False, "未翻译", 3)

class ocrIsMatchEnum(BaseEnum):
    ALL = (None, "全部", 1, "")
    MATCH = (True, "已匹配", 2)
    UNMATCH = (False, "未匹配", 3)

# 配置日志
logger = logging.getLogger(__name__)

# 定义常量 - 使用PathUtils从config.ini获取路径
REPOS_DIR = PathUtils.get_ocr_repos_dir()
UPLOADS_DIR = PathUtils.get_ocr_uploads_dir()
RESULTS_DIR = PathUtils.get_ocr_results_dir()


# 通用工具函数：语言命中与文本过滤（避免任何硬编码语言码）
def _is_language_hit(texts, target_languages):
    """判断识别结果是否命中任一目标语言。

    Args:
        texts (list[str]): 文本识别得到的文本列表，允许为空列表或None。
        target_languages (list[str] | None): 目标语言代码列表，例如 ['ch','en']。
            若为None或空列表，则默认使用 ['ch']。

    Returns:
        bool: 当且仅当 `texts` 中包含任一 `target_languages` 对应语言的文本时
        返回 True，否则返回 False。

    Raises:
        无显式抛出。内部异常会被吞并以保证稳健性。

    Example:
        >>> _is_language_hit(['你好','hello'], ['en'])
        True
        >>> _is_language_hit(['你好'], ['en'])
        False

    Notes:
        - 为保证健壮性，单项语言匹配异常不会影响整体判断，会被忽略继续。
        - 该函数不做语言码合法性校验，默认交由 `OCRService.check_language_match` 处理。
    """
    safe_langs = target_languages or ['ch']
    for lang in safe_langs:
        try:
            if OCRService.check_language_match(texts or [], lang):
                return True
        except Exception:
            # 单项语言匹配异常不影响整体判断
            continue
    return False


def _filter_texts_by_languages(texts, target_languages):
    """按目标语言过滤文本并返回命中项。

    Args:
        texts (list[str]): 文本识别得到的文本列表，允许为空列表或None。
        target_languages (list[str] | None): 目标语言代码列表，例如 ['ch','en']。
            若为None或空列表，则默认使用 ['ch']。

    Returns:
        list[str]: 所有被任一目标语言规则命中的文本项，按原顺序返回。

    Raises:
        无显式抛出。内部异常会被吞并以保证稳健性。

    Example:
        >>> _filter_texts_by_languages(['你好','hello'], ['en'])
        ['hello']

    Notes:
        - 使用 `OCRService.check_language_match` 对单条文本进行判定。
        - 单条判定异常不会中断流程，仅跳过该条。
    """
    safe_langs = target_languages or ['ch']
    filtered = []
    for text in texts or []:
        try:
            if any(OCRService.check_language_match([text], lang) for lang in safe_langs):
                filtered.append(text)
        except Exception:
            continue
    return filtered


@shared_task()
def process_ocr_task(task_id):
    """调度并处理指定 OCR 任务。

    Args:
        task_id (int|str): `OCRTask` 主键ID。

    Returns:
        dict: 执行结果字典，包含 `status` 与可选的 `task_id`/`message` 等。

    Raises:
        异常会被捕获记录到日志，并回写任务状态为 failed，不向外抛出。

    Example:
        由 Celery Worker 异步调用：
        >>> process_ocr_task.delay(123)

    Notes:
        - 批量写库逻辑在 `MultiThreadOCR` 内部完成。
        - 目标语言从 `task.config['target_languages']` 动态获取，未设置默认 ['ch']。
        - 命中规则统一在 `_is_language_hit` / `_filter_texts_by_languages` 中处理。
    """
    logger.info(f"开始处理OCR任务: {task_id}")

    try:
        # step1. 获取任务信息
        logger.info(f"开始查询OCR任务: {task_id}, 类型: {type(task_id)}")
        
        # 添加重试机制，处理数据库事务延迟问题
        task = None
        max_retries = 5
        retry_delay = 0.3  # 300毫秒
        
        for attempt in range(max_retries):
            logger.info(f"第{attempt + 1}次尝试查询任务: {task_id}")
            task = OCRTask.objects.all_teams().filter(id=task_id).first()
            if task:
                logger.info(f"✅ 第{attempt + 1}次尝试成功找到OCR任务: {task.id}")
                break
            
            # 查询失败，记录调试信息
            logger.warning(f"❌ 第{attempt + 1}次尝试未找到任务 {task_id}")
            
            if attempt < max_retries - 1:
                logger.warning(f"⏳ {retry_delay}秒后重试...")
                time.sleep(retry_delay)
                retry_delay *= 1.5  # 指数退避
            else:
                # 最后一次尝试失败，记录详细调试信息
                all_tasks = OCRTask.objects.all_teams().values_list('id', 'name', 'status', 'created_at')
                recent_tasks = list(all_tasks.order_by('-created_at')[:10])
                logger.error(f"OCR任务不存在: {task_id}")
                logger.error(f"数据库中最近10个任务: {recent_tasks}")
                logger.error(f"查询条件: id={task_id}")
                return {"status": "error", "message": f"OCR任务不存在: {task_id}"}
        
        logger.info(f"成功找到OCR任务: {task.id}, 名称: {task.name}, 状态: {task.status}")

        # 更新任务状态
        notify_ocr_task_progress({
            "id": task_id,
            "status": 'running',
            "start_time": timezone.now(),
        })

        # 获取目标语言
        task_config = task.config or {}
        target_languages = task_config.get('target_languages', ['ch'])  # 默认检测中文（官方语言码）
        logger.info(f"任务配置: {task_config}")
        logger.info(f"目标语言: {target_languages}")

        rec_score_raw = task_config.get('rec_score_thresh') # cong
        if rec_score_raw is None:
            rec_score_raw = task_config.get('rec score thresh', 0.5)
        try:
            rec_score_thresh = float(rec_score_raw)
        except (TypeError, ValueError):
            logger.error("任务配置识别阈值无效: %s", rec_score_raw)
            raise ValueError("任务配置识别阈值无效")
        if rec_score_thresh < 0 or rec_score_thresh > 1:
            logger.error("任务识别阈值超出范围: %.4f", rec_score_thresh)
            raise ValueError("任务识别阈值超出范围")
        logger.info("任务使用识别阈值: %.2f", rec_score_thresh)


        # 从命令行指定的配置文件读取OCR多线程配置
        config = settings.CFG._config
        ocr_max_workers = config.getint('ocr', 'ocr_max_workers', fallback=4)

        logger.warning(f"从配置文件读取OCR配置: max_workers={ocr_max_workers}, 配置文件: {settings.CFG._config_path}")

        # 为了方便调试，直接使用固定目录，不管任务类型
        debug_dir = PathUtils.get_debug_dir()
        debug_status = False

        # 打印完整的调试目录路径，方便排查问题
        logger.info(f"调试目录完整路径: {os.path.abspath(debug_dir)}")

        # step2. 获取待检测目录路径
        # 获取待检测目录路径 & 待检测文件相对路径
        target_dir = task_config.get("target_dir")
        target_path = task_config.get("target_path")
        check_dir = ""
        
        logger.info(f"任务配置 - target_dir: {target_dir}")
        logger.info(f"任务配置 - target_path: {target_path}")
        logger.info(f"任务配置 - source_type: {task.source_type}")

        # 检查调试目录是否存在 且 开启调试（快速使用指定目录图片排查识别逻辑时使用）
        if os.path.exists(debug_dir) and debug_status:
            logger.info(f"使用调试目录: {debug_dir}")
            check_dir = debug_dir
        else:
            # 如果调试目录不存在，则使用正常流程
            logger.warning(f"调试目录:[ {debug_dir} ] 不存在或未开启调试模式，使用正常流程")

            # 根据任务类型确定检查目录
            if task.source_type == 'upload':
                # 对于上传任务，target_dir是相对路径，需要与MEDIA_ROOT拼接
                check_dir = os.path.join(settings.MEDIA_ROOT, target_dir)
            elif task.source_type == 'git':
                # Git任务：动态获取repos基础目录，避免使用旧配置中的路径
                repos_base_dir = PathUtils.get_ocr_repos_dir()
                check_dir = os.path.join(repos_base_dir, target_path)
                logger.info(f"Git任务检查目录: {check_dir} (基础目录: {repos_base_dir}, 仓库名: {target_path})")
                git_service = GitLabService(
                    GitLabConfig(
                        repo_url=task.git_repository.url,
                        access_token=task.git_repository.token,
                    )
                )
                notify_ocr_task_progress({
                    "id": task_id,
                    "remark": "正在同步 Git 仓库...",
                })
                result: DownloadResult = git_service.download_files_with_git_clone(
                    repo_base_dir=check_dir,
                    branch=task_config.get("branch", "develop"),
                )
                if not result.success:
                    logger.error(f"Git仓库下载失败: {result.message}")
                    notify_ocr_task_progress({
                        "id": task_id,
                        "status": 'failed',
                        "end_time": timezone.now(),
                        "remark": f"Git仓库下载失败: {result.message}",
                    })
                    return {"status": "error", "message": f"Git仓库下载失败: {result.message}"}
            else:
                logger.error(f"不支持的任务类型: {task.source_type}")
                notify_ocr_task_progress({
                    "id": task_id,
                    "status": 'failed',
                    "end_time": timezone.now(),
                    "remark": f"不支持的任务类型: {task.source_type}",
                })
                return {"status": "error", "message": f"不支持的任务类型: {task.source_type}"}

        # 检查目标目录
        if not check_dir or not os.path.exists(check_dir):
            logger.error(f"OCR待检测目录不存在: {check_dir}")
            notify_ocr_task_progress({
                "id": task_id,
                "status": 'failed',
                "end_time": timezone.now(),
                "remark": f"OCR待检测目录不存在: {check_dir}",
            })
            return {"status": "error", "message": f"OCR待检测目录不存在: {check_dir}"}

        # step4. 执行主逻辑
        # 初始化多线程OCR服务
        logger.warning(f"初始化多线程OCR服务 ( 最大工作线程: {ocr_max_workers})")
        # multi_thread_ocr = MultiThreadOCR(
        #     task,
        #     lang="ch",  # 默认使用中文模型
        #     max_workers=ocr_max_workers,  # 使用配置的工作线程数
        #     match_languages=target_languages  # 将命中判定语言动态传入，避免硬编码
        # )

        # ================== OCR识别前先利用 Cache 过滤，避免重复识别  =========================
        enable_cache = task_config.get('enable_cache', True)
        img_exts_init = {'.jpg', '.jpeg', '.png', '.bmp', '.webp', '.tif', '.tiff'}
        total_images = 0
        hit_hashes = set()
        image_paths = []
        msg = ""
        
        if not enable_cache:
            for root_dir, _, files in os.walk(check_dir):
                for file_name in files:
                    file_ext = os.path.splitext(file_name)[1].lower()
                    if file_ext not in img_exts_init:
                        continue
                    total_images += 1
            msg = f"未启用缓存, 待处理图片: {total_images}"
            logger.warning(msg)
        else:
            # 初始化进度(以待处理图片总数为准)
            abspath_to_hash = dict()
            try:
                notify_ocr_task_progress({
                "id": task_id,
                "remark": "正在使用OCR缓存进行预过滤...",
                })

                for root_dir, _, files in os.walk(check_dir):
                    for file_name in files:
                        file_ext = os.path.splitext(file_name)[1].lower()
                        if file_ext not in img_exts_init:
                            continue
                        img_abspath = os.path.join(root_dir, file_name)
                        img_hash = OCRService.calculate_image_hash(img_abspath)
                        abspath_to_hash[img_abspath] = img_hash
                        total_images += 1

                # 尝试命中缓存
                all_hashes_list = list(abspath_to_hash.values())
                hit_hashes = OCRCacheHit.try_hit(all_hashes_list, task_id=task_id)
                image_paths = [img_path for img_path, h in abspath_to_hash.items() if h not in hit_hashes]

                if len(image_paths) == 0:
                    logger.warning("⚡所有图片均命中OCR缓存, 无需重复识别")
                    task.calculate_match_rate_by_related_results()
                    notify_ocr_task_progress({
                        "id": task_id,
                        "status": 'completed',
                        "end_time": timezone.now(),
                        "total_images": total_images,
                        "verified_images": task.total_verified,
                        "processed_images": total_images,
                        "remark": f"✅ 任务执行完毕",
                    })
                    return {"status": "success", "task_id": task_id}

                msg = f"⚡缓存过滤完成: T{total_images};H{len(hit_hashes)};P{len(image_paths)}"
                logger.info(msg)
            except Exception as _init_prog_err:
                logger.warning(f"使用OCR缓存进行预过滤出错: {_init_prog_err}")
                image_paths = []
        notify_ocr_task_progress({
            "id": task_id,
            "total_images": total_images,
            "remark": msg,
        })
        # ==========================================================================

        # 使用两阶段OCR检测服务，集成了调优后的参数配置
        # 从任务配置中获取性能配置名称
        performance_config_name = task_config.get('performance_config', 'balanced')
        logger.warning(f"初始化两阶段OCR服务，性能配置: {performance_config_name}")

        # 从任务配置读取开关（未设置默认False）
        try:
            enable_draw = bool(task_config.get('rounds_draw_enable', False))
        except Exception:
            enable_draw = False
        try:
            enable_copy = bool(task_config.get('rounds_copy_enable', False))
        except Exception:
            enable_copy = False
        try:
            enable_annotate = bool(task_config.get('rounds_annotate_enable', False))
        except Exception:
            enable_annotate = False

        start_time = time.time()
        
        # 初始化两阶段OCR服务（默认不启用详细报告）
        two_stage_service = TwoStageOCRService(
            performance_config_name,
            enable_detailed_report=False,
            rec_score_thresh=rec_score_thresh
        )
        
        # 准备输入图片列表
        if image_paths:
            # 使用缓存过滤后的图片列表，同时过滤图片格式
            img_exts = {'.jpg', '.jpeg', '.png'}
            input_images = [
                img_path for img_path in image_paths
                if os.path.splitext(img_path)[1].lower() in img_exts
            ]
            if len(input_images) < len(image_paths):
                logger.info(f"格式过滤: 原始={len(image_paths)}, 保留={len(input_images)}, "
                           f"过滤={len(image_paths) - len(input_images)}")
        else:
            # 扫描目录获取所有图片（仅限jpg、jpeg、png格式）
            input_images = []
            img_exts = {'.jpg', '.jpeg', '.png'}
            for root_dir, _, files in os.walk(check_dir):
                for file_name in files:
                    if os.path.splitext(file_name)[1].lower() in img_exts:
                        input_images.append(os.path.join(root_dir, file_name))
        
        # 执行两阶段OCR检测
        ocr_lang = target_languages[0] if target_languages else "ch"
        logger.warning(f"🔍 执行OCR检测，使用语言: {ocr_lang}, 原始语言列表: {target_languages}")
        
        # 通知开始OCR检测
        notify_ocr_task_progress({
            "id": task_id,
            "remark": f"开始OCR检测，共{len(input_images)}张图片，使用{ocr_lang}模型...",
        })
        
        # 定义进度回调函数
        def ocr_progress_callback(processed, total, stage):
            """OCR检测进度回调"""
            progress_percent = int((processed / total * 100)) if total > 0 else 0
            notify_ocr_task_progress({
                "id": task_id,
                "processed_images": processed,
                "remark": f"{stage}: {processed}/{total} ({progress_percent}%)",
            })
        
        detection_result = two_stage_service.process_two_stage_detection(
            input_images, 
            lang=ocr_lang,
            progress_callback=ocr_progress_callback
        )
        
        end_time = time.time()
        elapsed_time = end_time - start_time

        # 检查检测结果
        if not detection_result or not isinstance(detection_result, dict):
            error_msg = "两阶段OCR检测失败"
            logger.error(error_msg)
            notify_ocr_task_progress({
                "id": task_id,
                "status": 'failed',
                "end_time": timezone.now(),
                "remark": error_msg,
            })
            return {"status": "error", "message": error_msg}

        # 获取检测结果统计
        final_stats = detection_result.get('final_statistics', {})
        total_hits = final_stats.get('total_hits', 0)
        final_miss = final_stats.get('final_miss', 0)
        overall_hit_rate = final_stats.get('overall_hit_rate', 0)
        
        logger.warning(
            f"两阶段OCR检测完成，总命中={total_hits} 最终未命中={final_miss} 命中率={overall_hit_rate:.1f}%，"
            f"耗时 {elapsed_time:.2f} 秒"
        )
        notify_ocr_task_progress({
            "id": task_id,
            "remark": f"两阶段OCR检测完成，耗时 {elapsed_time:.2f} 秒, 结果统计中...",
        })

        # 构建与旧流程兼容的结果列表（用于写库与汇总）
        # 从两阶段检测结果中获取所有命中记录和未命中路径
        all_hits_records = detection_result.get('all_hits_records', [])
        final_miss_paths = detection_result.get('final_statistics', {}).get('final_miss_paths', [])
        media_root = settings.MEDIA_ROOT

        # 生成兼容结果：将两阶段检测结果转换为数据库格式
        notify_ocr_task_progress({
            "id": task_id,
            "remark": f"正在处理OCR结果，命中={total_hits}张，未命中={final_miss}张...",
        })
        
        ocr_results = []
        
        # 1. 处理命中的记录
        for hit_record in all_hits_records:
            input_path = hit_record.get('input_path', '')
            texts = hit_record.get('rec_texts', [])
            confidences = hit_record.get('rec_scores', [])
            stage = hit_record.get('stage', 'unknown')
            
            # 计算相对路径（确保使用绝对路径）
            abs_input_path = os.path.abspath(input_path)
            abs_media_root = os.path.abspath(media_root)
            
            # 调试：记录第一张图片的路径信息
            if len(ocr_results) == 0:
                logger.info(f"=== 路径调试信息 ===")
                logger.info(f"原始路径: {input_path}")
                logger.info(f"绝对路径: {abs_input_path}")
                logger.info(f"Media根目录: {abs_media_root}")
                logger.info(f"是否以Media开头: {abs_input_path.startswith(abs_media_root)}")
            
            # 检查路径是否在media目录下
            if abs_input_path.startswith(abs_media_root):
                rel_path = os.path.relpath(abs_input_path, abs_media_root).replace('\\', '/')
            else:
                logger.warning(f"图片路径不在media目录下: {abs_input_path}")
                logger.warning(f"media_root: {abs_media_root}")
                rel_path = os.path.relpath(abs_input_path, abs_media_root).replace('\\', '/')
            
            # 读取图片分辨率
            pic_resolution = ''
            try:
                import numpy as _np
                import cv2 as _cv2
                data = _np.fromfile(input_path, dtype=_np.uint8)
                img_nd = _cv2.imdecode(data, _cv2.IMREAD_COLOR)
                if img_nd is not None:
                    h, w = img_nd.shape[:2]
                    pic_resolution = f"{int(w)}x{int(h)}"
            except Exception:
                pic_resolution = ''
            
            # 创建 OCR 结果记录
            ocr_results.append({
                'image_path': rel_path,
                'texts': texts,
                'confidences': confidences,
                'has_match': hit_record.get('has_match', True),  # 从检测结果获取命中状态
                'pic_resolution': pic_resolution,
                'stage': stage,  # 记录检测阶段
                'max_confidence': hit_record.get('max_rec_score', 0.0),
            })
        
        # 2. 处理未命中的记录（没有识别到文本的图片）
        for miss_path in final_miss_paths:
            # 计算相对路径
            abs_miss_path = os.path.abspath(miss_path)
            abs_media_root = os.path.abspath(media_root)
            
            if abs_miss_path.startswith(abs_media_root):
                rel_path = os.path.relpath(abs_miss_path, abs_media_root).replace('\\', '/')
            else:
                logger.warning(f"未命中图片路径不在media目录下: {abs_miss_path}")
                rel_path = os.path.relpath(abs_miss_path, abs_media_root).replace('\\', '/')
            
            # 读取图片分辨率
            pic_resolution = ''
            try:
                import numpy as _np
                import cv2 as _cv2
                data = _np.fromfile(miss_path, dtype=_np.uint8)
                img_nd = _cv2.imdecode(data, _cv2.IMREAD_COLOR)
                if img_nd is not None:
                    h, w = img_nd.shape[:2]
                    pic_resolution = f"{int(w)}x{int(h)}"
            except Exception:
                pic_resolution = ''
            
            # 创建未命中的OCR结果记录
            ocr_results.append({
                'image_path': rel_path,
                'texts': [],  # 未命中，没有文本
                'confidences': [],  # 未命中，没有置信度
                'has_match': False,  # 未命中
                'pic_resolution': pic_resolution,
                'stage': 'miss',  # 标记为未命中
                'max_confidence': 0.0,
            })

        # 关键字过滤（如果启用）
        keyword_filter_config = task_config.get('keyword_filter', {})
        logger.info(f"关键字过滤配置: enabled={keyword_filter_config.get('enabled', False)}")
        
        if keyword_filter_config.get('enabled'):
            notify_ocr_task_progress({
                "id": task_id,
                "remark": f"开始关键字过滤，共{len(ocr_results)}条结果...",
            })
            from apps.ocr.services.keyword_filter import KeywordFilter
            keyword_filter = KeywordFilter(keyword_filter_config)
            original_count = len(ocr_results)
            ocr_results = keyword_filter.filter_results(ocr_results)
            logger.info(f"关键字过滤: 原始结果={original_count}, 过滤后={len(ocr_results)}")
            
            notify_ocr_task_progress({
                "id": task_id,
                "remark": f"关键字过滤完成: 原始={original_count}, 匹配={len(ocr_results)}",
            })
        else:
            logger.info("关键字过滤未启用 (enabled=False)")
            notify_ocr_task_progress({
                "id": task_id,
                "remark": f"跳过关键字过滤，共{len(ocr_results)}条结果",
            })
        
        # 保存两阶段检测结果到文件（用于调试）
        try:
            import json as _json
            report_dir = os.path.join(settings.MEDIA_ROOT, 'ocr', 'reports')
            os.makedirs(report_dir, exist_ok=True)
            
            # 分离命中和未命中的记录
            hits_only_result = {
                "all_hits_records": detection_result.get('all_hits_records', []),
                "final_statistics": detection_result.get('final_statistics', {}),
            }
            
            # 保存命中结果
            result_file = os.path.join(report_dir, f"{task.id}_two_stage_result.json")
            with open(result_file, 'w', encoding='utf-8') as fp:
                _json.dump(hits_only_result, fp, ensure_ascii=False, indent=2)
            logger.warning(f"两阶段检测结果已写入: {result_file}")
            
            # 保存未命中结果（如果有）
            miss_records = detection_result.get('all_miss_records', [])
            if miss_records:
                miss_file = os.path.join(report_dir, f"{task.id}_miss_details.json")
                miss_result = {
                    "total_miss": len(miss_records),
                    "miss_records": miss_records,
                }
                with open(miss_file, 'w', encoding='utf-8') as fp:
                    _json.dump(miss_result, fp, ensure_ascii=False, indent=2)
                logger.warning(f"未命中详情已写入: {miss_file}")
        except Exception as _result_err:
            logger.warning(f"写入两阶段检测结果失败(忽略): {_result_err}")

        if not ocr_results:
            logger.warning("未检测到任何图片，任务结束")
            notify_ocr_task_progress({
                "id": task_id,
                "status": 'completed',
                "end_time": timezone.now(),
                "total_images": len(input_images),
                "processed_images": 0,
                "matched_images": 0,
                "match_rate": 0.0,
                "remark": "未检测到任何图片，任务结束",
            })
            return {"status": "success", "task_id": task_id}

        # 批量记录 OCRResult & 统计最终命中数
        notify_ocr_task_progress({
            "id": task_id,
            "remark": f"正在保存OCR结果到数据库，共{len(ocr_results)}条...",
        })
        
        new_results = []
        total_matches = 0
        for item in ocr_results:
            img_full_path = os.path.join(media_root, item['image_path'])
            img_hash = OCRService.calculate_image_hash(img_full_path)
            if enable_cache and img_hash in hit_hashes:
                # 如果本次任务启用缓存并且此缓存已存在，则跳过写库
                continue
            obj = OCRResult(
                task=task,
                image_hash=img_hash,
                image_path=item.get('image_path', '').replace('\\', '/'),
                texts=item.get('texts', []),
                languages=item.get('languages', {}),
                has_match=item.get('has_match', False),
                confidences=item.get('confidences', []),
                max_confidence=item.get('max_confidence', 0.0),
                processing_time=item.get('processing_time', 0),
                pic_resolution=item.get('pic_resolution', ''),
                team_id=task.team_id
            )
            new_results.append(obj)
            texts_present = bool(item.get('texts'))
            if texts_present and item.get('has_match', False):
                total_matches += 1

        OCRResult.objects.bulk_create(new_results)
        logger.warning(f"批量插入 {len(new_results)} 条OCR结果到数据库")
        
        notify_ocr_task_progress({
            "id": task_id,
            "verified_images": task.total_verified,
            "remark": f"已保存{len(new_results)}条结果到数据库",
        })
        
        # 强制提交数据库事务
        from django.db import transaction
        transaction.commit()
        
        # 短暂延迟确保数据库操作完全完成
        time.sleep(0.2)
        
        # 记录ocr缓存
        OCRCache.record_cache(task_id)


        # 生成汇总报告
        notify_ocr_task_progress({
            "id": task_id,
            "remark": "正在生成汇总报告...",
        })
        
        logger.warning("开始生成汇总报告")
        _generate_summary_report(task, ocr_results, target_languages)
        logger.warning("汇总报告生成完成")
        
        notify_ocr_task_progress({
            "id": task_id,
            "remark": "汇总报告生成完成",
        })

        # 完成进度 - 直接使用已知的统计数据更新任务
        try:
            logger.warning(f"开始更新任务 {task_id} 的统计数据...")
            
            # 直接计算统计数据，不依赖复杂的查询
            total_processed = len(ocr_results)
            total_matched = sum(1 for item in ocr_results if item.get('has_match', False))
            match_rate = round((total_matched / total_processed * 100), 2) if total_processed > 0 else 0.0
            
            # 直接更新任务统计字段
            task.processed_images = total_processed
            task.matched_images = total_matched
            task.match_rate = match_rate
            task.save(update_fields=["processed_images", "matched_images", "match_rate"])
            
            logger.warning(f"任务 {task_id} 统计数据更新完成: 总数={total_processed}, 匹配数={total_matched}, 匹配率={match_rate}%")
            
            notify_ocr_task_progress({
                "id": task_id,
                "status": 'completed',
                "end_time": timezone.now(),
                "total_images": total_processed,
                "processed_images": total_processed,
                "matched_images": total_matched,
                "match_rate": match_rate,
                "remark": "✅ 任务执行完毕",
            })
        except Exception as _fin_err:
            logger.warning(f"完成进度更新失败(忽略): {_fin_err}")

        return {"status": "success", "task_id": task_id}

    except Exception as e:
        logger.error(f"任务处理失败: {str(e)}")
        # 记录详细的异常堆栈
        import traceback
        logger.error(traceback.format_exc())

        # 更新任务状态为失败
        try:
            notify_ocr_task_progress({
                "id": task_id,
                "status": 'failed',
                "end_time": timezone.now(),
                "remark": f"任务处理失败: {str(e)}",
            })
        except Exception:
            pass

        return {"status": "error", "message": str(e)}


def _generate_summary_report(task, results, target_languages):
    """生成OCR汇总报告（按动态目标语言命中）。

    Args:
        task (OCRTask): 当前任务实例。
        results (list[dict]): 识别结果列表，元素包含 `image_path`、`texts` 等字段。
        target_languages (list[str] | None): 目标语言代码列表；None/空默认 ['ch']。

    Returns:
        None: 结果直接写入报告文件，并更新 `task.config['report_file']`。

    Raises:
        无显式抛出。内部异常已做保护性处理并记录日志。

    Example:
        >>> _generate_summary_report(task, results, ['ch','en'])

    Notes:
        - 命中判定遵循“任意目标语言命中即视为命中”。
        - 路径统一经 `PathUtils.normalize_path` 规范化。
        - 将在 `MEDIA_ROOT/ocr/reports/` 目录生成两个文件:
          1) `{task.id}_ocr_summary.json` 本次任务的结构化汇总
          2) `ocr_summary.json` 指向最近一次生成的覆盖式汇总
    """
    # 统计信息
    total_images = len(results)
    matched_images = []

    # 筛选包含目标语言的图片（命中规则：包含任一目标语言文字即为命中）
    for result in results:
        # 跳过处理失败的图片
        if 'error' in result:
            continue

        texts = result.get('texts', [])
        if not _is_language_hit(texts, target_languages):
            continue

        # 获取文件信息
        image_path = result.get('image_path', '')
        image_path = PathUtils.normalize_path(image_path)
        file_name = os.path.basename(image_path)

        try:
            # 尝试获取文件大小（相对路径拼接 MEDIA_ROOT）
            if os.path.isabs(image_path):
                file_size = os.path.getsize(image_path) / 1024  # KB
            else:
                full_path = os.path.join(settings.MEDIA_ROOT, image_path)
                file_size = os.path.getsize(full_path) / 1024  # KB
        except Exception:
            file_size = 0

        matched_texts = _filter_texts_by_languages(texts, target_languages)

        matched_images.append({
            'path': image_path,
            'name': file_name,
            'size': file_size,
            'time': result.get('time_cost', 0),
            'texts': texts,
            'matched_texts': ' '.join(matched_texts),
        })

    # 计算统计信息
    matched_count = len(matched_images)
    matched_rate = (matched_count / total_images * 100) if total_images > 0 else 0

    # 生成报告内容（动态目标语言）
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    langs_str = ','.join(target_languages or ['ch'])
    report = f"""包含目标语言的图片检测结果
==================================================
生成时间: {now}
目标语言: {langs_str}
总处理图片数: {total_images}
命中图片数: {matched_count}
命中率: {matched_rate:.1f}%

命中的图片列表:
------------------------------"""

    # 添加每张图片的详细信息
    for i, img in enumerate(matched_images, 1):
        report += f"""
{i}. 图片信息:
   文件路径: {img['path']}
   文件名: {img['name']}
   文件大小: {img['size']:.2f} KB
   处理时间: {img['time']:.2f}秒
   命中文本: {img['matched_texts']}
   完整文本: {' '.join(img['texts'])}"""

    # 添加说明信息
    report += """

1、识别结束后的结果按照以上格式汇总输出。
2、不再单张图片单个文件输出。"""

    # 保存报告
    report_dir = PathUtils.get_ocr_reports_dir()
    os.makedirs(report_dir, exist_ok=True)

    # 同步输出结构化 JSON 汇总，便于前端或其他工具使用
    try:
        import json
        json_items = []
        for img in matched_images:
            json_items.append({
                'path': img['path'],
                'name': img['name'],
                'matched_texts': img['matched_texts'],
                # 新增字段, 从结果回填: 分辨率、像素、参数差异、置信度、模式
                'resolution': None,
                'pixels': None,
                'param_diff': None,
                'confidences': None,
                'mode_display': None,
            })
        # 若原始 results 中包含 used_preset/mode_display, 则进行对齐合并
        try:
            # 建立 path -> extras 的映射
            extras = {}
            for r in results:
                if 'error' in r:
                    continue
                p = PathUtils.normalize_path(r.get('image_path', ''))
                extras[p] = {
                    'mode_display': r.get('mode_display'),
                    'resolution': r.get('resolution'),
                    'pixels': r.get('pixels'),
                    'param_diff': r.get('param_diff'),
                    'confidences': r.get('confidences'),
                }
            # 回填
            for item in json_items:
                ext = extras.get(item['path']) or {}
                if ext.get('mode_display') is not None:
                    item['mode_display'] = ext.get('mode_display')
                if ext.get('resolution') is not None:
                    item['resolution'] = ext.get('resolution')
                if ext.get('pixels') is not None:
                    item['pixels'] = ext.get('pixels')
                if ext.get('param_diff') is not None:
                    item['param_diff'] = ext.get('param_diff')
                if ext.get('confidences') is not None:
                    item['confidences'] = ext.get('confidences')
        except Exception:
            pass

        summary_json = {
            'task_id': task.id,
            'generated_at': now,
            'target_languages': target_languages or ['ch'],
            'total_images': total_images,
            'matched_count': matched_count,
            'matched_rate': round(matched_rate, 2),
            'items': json_items,
        }
        json_dir = report_dir
        json_file = os.path.join(json_dir, f"{task.id}_ocr_summary.json")
        with open(json_file, 'w', encoding='utf-8') as jf:
            json.dump(summary_json, jf, ensure_ascii=False, indent=2)
        # 生成覆盖式别名文件, 方便用户直接查找最近一次的汇总
        alias_file = os.path.join(json_dir, "ocr_summary.json")
        with open(alias_file, 'w', encoding='utf-8') as af:
            json.dump(summary_json, af, ensure_ascii=False, indent=2)
        # 使用 warning 级别便于在控制台看到完整路径
        logger.warning(f"OCR JSON汇总已生成: {json_file}")
        logger.warning(f"OCR 最近一次汇总(覆盖): {alias_file}")
    except Exception as je:
        logger.error(f"生成OCR JSON汇总失败: {je}")

    # 仅生成JSON, 不再生成txt; 保持任务其他信息不变

    logger.info(
        f"OCR识别统计: 总图片数={total_images}, 命中图片数={matched_count}, 命中率={matched_rate:.1f}%"
    )


def _process_results(task, results, target_languages):
    """
    处理OCR识别结果（通用动态语言命中）。

    Args:
        task (OCRTask): 当前任务实例。
        results (list[dict]): 识别结果列表，元素包含 `image_path`、`texts` 等字段。
        target_languages (list[str] | None): 目标语言代码列表；None/空默认 ['ch']。

    Returns:
        dict: 包含以下键：
            - processed_results (list[dict]): 附带 `languages`/`has_match` 的结果列表
            - matched_images (list[dict]): 命中图片的精简信息
            - matched_count (int): 命中数量
            - matched_rate (float): 命中率（0~100）

    Raises:
        无显式抛出。内部异常会在必要处被保护性捕获并记录。

    Example:
        >>> data = _process_results(task, results, ['ch','en'])
        >>> data['matched_count']

    Notes:
        - 命中规则：只要包含任一目标语言文本即命中。
        - 避免硬编码语言；按输入动态决定。
        - 严格控制缩进与循环层级，保证可维护性与可读性。
    """
    logger.warning(f"开始处理OCR结果，共 {len(results)} 个结果")

    processed_results = []
    matched_images = []

    safe_langs = target_languages or ['ch']

    for result in results:
        if 'error' in result:
            continue

        texts = result.get('texts', [])
        languages = {}
        for lang in safe_langs:
            try:
                if OCRService.check_language_match(texts, lang):
                    languages[lang] = True
            except Exception:
                continue
        has_match = bool(languages)

        enriched = {
            **result,
            'languages': languages,
            'has_match': has_match,
        }
        processed_results.append(enriched)

        if has_match:
            image_path = PathUtils.normalize_path(result.get('image_path', ''))
            file_name = os.path.basename(image_path)
            try:
                if os.path.isabs(image_path):
                    file_size = os.path.getsize(image_path) / 1024  # KB
                else:
                    full_path = os.path.join(settings.MEDIA_ROOT, image_path)
                    file_size = os.path.getsize(full_path) / 1024  # KB
            except Exception:
                file_size = 0

            matched_texts = _filter_texts_by_languages(texts, safe_langs)

            matched_images.append({
                'path': image_path,
                'name': file_name,
                'size': file_size,
                'time': result.get('time_cost', 0),
                'texts': texts,
                'matched_texts': ' '.join(matched_texts),
            })

    total_images = len(results)
    matched_count = len(matched_images)
    matched_rate = (matched_count / total_images * 100) if total_images > 0 else 0

    logger.info(
        f"OCR识别统计: 总图片数={total_images}, 命中图片数={matched_count}, 命中率={matched_rate:.1f}%"
    )

    # 可按需返回结构化结果，当前不影响既有调用方
    return {
        'processed_results': processed_results,
        'matched_images': matched_images,
        'matched_count': matched_count,
        'matched_rate': matched_rate,
    }

@shared_task()
def binding_translated_image(task_id: str):
    """
    OCR 任务再次发起对比处理任务，对比的对象为：
    a. task_id 生成的 OcrResult 结果
    b. 用户指定的远程 Git 仓库、分支、路径下的所有文件
    帮助用户在 OcrResult 的每个记录中标识出是否在远程仓库中存在对应的已翻译的文件。
    Args:
        task_id (int): OCR 任务 ID。
    """
    logger.info(f"开始处理对比任务: {task_id}")

    def update_trans_repo_status(status, error=""):
        """更新翻译仓库关联状态"""
        if task.config and 'trans_repo' in task.config:
            task.config['trans_repo']['status'] = status
            task.config['trans_repo']['error'] = error
            notify_ocr_task_progress({
                "id": task_id,
                "config": task.config,
            })
    
    try:
        # step1. 一些预校验
        task: OCRTask = OCRTask.objects.all_teams().filter(id=task_id).first()
        if not task:
            logger.error(f"OCR任务不存在: id={task_id}")
            return

        task_config = task.config or {}
        trans_repo = task_config.get('trans_repo')
        if not trans_repo:
            msg = f"OCR任务未配置翻译仓库对照参数: id={task_id}"
            logger.error(msg)
            return

        try:
            trans_repo_config = TransRepoConfig(**trans_repo)
        except Exception as e:
            msg = f"OCR任务对比仓库配置错误: {str(e)}"
            logger.error(msg)
            update_trans_repo_status("failed", msg)
            return

        results = list(task.related_results) # 转换为列表以便多次使用
        if len(results) == 0:
            logger.warning(f"OCR任务无相关结果，跳过对比处理: id={task_id}")
            update_trans_repo_status("completed")
            return

        # step2. 将翻译仓库克隆到本地目录

        download_result = trans_repo_config.download_repo()
        if not download_result.success:
            msg = f"对比仓库下载失败: {download_result.message}"
            logger.error(msg)
            update_trans_repo_status("failed", msg)
            return

        # step3. 建立索引 (性能优化关键点)
        try:
            # 预先扫描目录建立 Hash Set 索引，避免后续循环中频繁 IO
            trans_repo_config.build_repo_index()
        except Exception as e:
            msg = f"建立索引失败: {e}"
            logger.error(msg)
            update_trans_repo_status("failed", msg)
            return

        # step4. 遍历 OCR 结果：利用索引匹配
        update_results = []
        match_count = 0
        
        for ocr_result in results:
            # 使用 match_image_path (内存查找) 代替 locate_trans_image_path (磁盘查找)
            trans_image_path = trans_repo_config.match_image_path(ocr_result.image_path)
            
            # 只有状态改变或路径改变时才更新，或者全部更新
            ocr_result.is_translated = trans_image_path is not None
            ocr_result.trans_image_path = trans_image_path
            update_results.append(ocr_result)
            
            if ocr_result.is_translated:
                match_count += 1

        # step5. 批量更新结果
        if update_results:
            OCRResult.objects.bulk_update(
                update_results,
                fields=['is_translated', 'trans_image_path']
            )
        
        # 更新状态为完成
        update_trans_repo_status("completed")
        
        logger.info(f"对比完成: id={task_id}, 总数={len(results)}, 匹配={match_count}")
        
        # 发送完成通知

    except Exception as e:
        logger.error(f"对比任务执行异常: {str(e)}")
        logger.error(traceback.format_exc())
        update_trans_repo_status("failed", str(e))



@shared_task()
def export_offline_html_task(task_id: str):
    """
    导出离线HTML报告任务
    """
    logger.info(f"开始导出离线报告: {task_id}")
    try:
        task = OCRTask.objects.all_teams().get(id=task_id)
        
        # 序列化数据
        task_data = OCRTaskSerializer(task).data
        
        # 分批处理结果，避免一次性加载过多数据导致内存溢出
        results_data = []
        batch_size = 50  # 每批处理的数量
        total_count = task.related_results.count()
        
        logger.info(f"待处理结果总数: {total_count}，批次大小: {batch_size}")
        
        # 使用游标分页或切片进行分批处理
        # 这里使用切片，对于几万条数据是可以接受的
        for start in range(0, total_count, batch_size):
            end = min(start + batch_size, total_count)
            # 获取当前批次的数据
            batch_results = task.related_results.all().order_by('id')[start:end]
            batch_data = OCRResultSerializer(batch_results, many=True).data
            
            # 处理当前批次的图片转Base64
            for item in batch_data:
                # 定义压缩函数
                def compress_image(path):
                    if not path: return ""
                    abs_path = os.path.join(settings.MEDIA_ROOT, path)
                    if not os.path.exists(abs_path): return ""
                    
                    try:
                        with Image.open(abs_path) as img:
                            buffer = io.BytesIO()
                            # 优先尝试 WebP 格式 (体积小且质量好，特别适合带文字的截图)
                            try:
                                # 如果是 RGBA 模式，WebP 可以保留透明度；如果是其他模式转 RGB
                                if img.mode not in ('RGB', 'RGBA'):
                                    img = img.convert('RGB')
                                
                                # quality=75: 视觉无损
                                # method=4: 默认压缩速度/质量平衡
                                img.save(buffer, format="WEBP", quality=75, method=4)
                                mime_type = "image/webp"
                            except Exception:
                                # 回退到 JPEG
                                buffer.seek(0)
                                buffer.truncate()
                                if img.mode != 'RGB':
                                    img = img.convert('RGB')
                                # quality=75: 保证清晰度
                                # subsampling=0: 关闭色度抽样，防止文字边缘颜色失真(变红/变糊)
                                # optimize=True: 优化 Huffman 表，减小体积
                                img.save(buffer, format="JPEG", quality=75, optimize=True, subsampling=0)
                                mime_type = "image/jpeg"
                                
                            img_str = base64.b64encode(buffer.getvalue()).decode()
                            return f"data:{mime_type};base64,{img_str}"
                    except Exception as e:
                        logger.error(f"图片处理失败 {abs_path}: {e}")
                        return ""

                # 处理原图
                item['image_url'] = compress_image(item.get('image_path'))
                
                # 处理翻译图
                item['trans_image_url'] = compress_image(item.get('trans_image_path'))
            
            # 将处理好的批次数据添加到总列表中
            results_data.extend(batch_data)
            
            # 打印进度
            progress = (end / total_count) * 100
            logger.info(f"导出进度: {end}/{total_count} ({progress:.1f}%)")

        # 辅助函数：将枚举类转换为前端可用字典
        def enum_to_dict(enum_cls):
            res = {}
            for name, member in enum_cls.__members__.items():
                res[name] = {
                    'value': member.value,
                    'label': member.label,
                    'order': getattr(member, 'order', 0)
                }
            return res

        enums_data = {
            'ocrResultTypeEnum': enum_to_dict(ocrResultTypeEnum),
            'ocrIsMatchEnum': enum_to_dict(ocrIsMatchEnum),
            'ocrIsVerifiedEnum': enum_to_dict(ocrIsVerifiedEnum),
            'ocrIsTranslatedEnum': enum_to_dict(ocrIsTranslatedEnum),
        }

        # 渲染模板
        context = {
            'task': task_data,
            'task_json': json.dumps(task_data),
            'results_json': json.dumps(results_data),
            'enums_json': json.dumps(enums_data)
        }
        
        html_content = render_to_string('ocr/offline_report.html', context)
        
        # 保存文件
        report_dir = PathUtils.get_ocr_reports_dir()
        os.makedirs(report_dir, exist_ok=True)
        file_name = f"ocr_report_{task.id}_offline.html"
        file_path = os.path.join(report_dir, file_name)
        
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(html_content)
            
        logger.info(f"离线报告生成成功: {file_path}")
        
        # 发送通知（可选，如果前端通过轮询或SSE接收）
        notify_ocr_task_progress({
            "id": task_id,
            "offline_report_url": f"/media/ocr/reports/{file_name}",
            "msg": "离线报告生成成功"
        })
        
    except Exception as e:
        logger.error(f"导出离线报告失败: {e}")
        logger.error(traceback.format_exc())





"""
OCR识别服务
封装PP-OCRv5引擎，提供图片文字识别功能
"""
import hashlib
import os
import configparser
import logging
from pathlib import Path
from typing import List, Dict, Optional, Union, Any, Tuple
import datetime
from django.conf import settings
from django.utils import timezone
import json
import re
import time
import uuid
import numpy as np
import cv2
import sys
import ctypes
import importlib.util
from .path_utils import PathUtils
from .performance_config import get_performance_config, PARAM_VERSIONS
import threading
from paddlex import create_pipeline
from paddleocr import PaddleOCR
import shutil
from apps.notifications.tasks import notify_ocr_task_progress
# import paddle

import warnings
warnings.filterwarnings("ignore", message="iCCP: known incorrect sRGB profile")

# 配置日志
logger = logging.getLogger(__name__)


# 读取配置
# config = PathUtils.load_config()
config = settings.CFG._config

# OCR相关路径配置
RESULTS_DIR = PathUtils.get_ocr_results_dir()
UPLOADS_DIR = PathUtils.get_ocr_uploads_dir()
REPOS_DIR = PathUtils.get_ocr_repos_dir()

# OCR引擎配置（保留参数占位，不在 PaddleOCR 初始化时直接使用 device）
GPU_ENABLED = config.getboolean('ocr', 'gpu_enabled', fallback=True)
OCR_DEFAULT_LANG = config.get('ocr', 'ocr_default_lang', fallback='ch')

# 预先设置CUDA环境变量，确保在任何导入前优先使用NVIDIA显卡
if GPU_ENABLED:
    # 强制使用NVIDIA显卡
    os.environ["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
    # 禁用集成显卡
    os.environ["CUDA_VISIBLE_DEVICES"] = "0"  # 假设NVIDIA显卡是设备0
    # 设置Paddle使用GPU
    os.environ["FLAGS_use_gpu"] = "true"
    os.environ["FLAGS_fraction_of_gpu_memory_to_use"] = "0.8"  # 使用80%的GPU显存
    # 禁用TensorFlow可能使用的集成显卡
    os.environ["TF_FORCE_GPU_ALLOW_GROWTH"] = "true"
    # 禁用英特尔集成显卡
    os.environ["OPENCV_DNN_OPENCL_ALLOW_ALL_DEVICES"] = "0"
    # 禁用英特尔MKL
    os.environ["MKL_SERVICE_FORCE_INTEL"] = "0"

    # 检测NVIDIA GPU
    # try:
    #     import subprocess
    #     nvidia_smi = subprocess.run(['nvidia-smi'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    #     if nvidia_smi.returncode == 0:
    #         logger.warning(f"NVIDIA GPU信息:\n{nvidia_smi.stdout}")
    #     else:
    #         logger.warning(f"无法获取NVIDIA GPU信息: {nvidia_smi.stderr}")
    # except Exception as e:
    #     logger.warning(f"检测NVIDIA GPU失败: {e}")


class OCRInstancePool:
    """OCR实例池，用于缓存不同参数组合的OCR实例，避免频繁重建
    
    集成了性能优化配置和两阶段检测支持
    """

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if not getattr(self, '_initialized', False):
            # OCR实例缓存：键为参数组合的hash，值为OCR实例
            self._cache = {}
            # 缓存访问锁
            self._cache_lock = threading.Lock()
            # 最大缓存数量（避免内存占用过多）
            self._max_cache_size = 12
            # 使用统计，用于LRU淘汰
            self._usage_stats = {}
            self._initialized = True
            logger.info("OCR实例池初始化完成")

    def _generate_cache_key(self, lang: str, stage: str = "baseline",
                            use_fast_models: bool = False) -> str:
        """生成缓存键，基于语言、检测阶段和模型类型
        
        Args:
            lang: 语言代码
            stage: 检测阶段 (baseline 或 balanced_v1)
            use_fast_models: 是否使用快速模型
        """
        model_type = 'mobile' if use_fast_models else 'server'
        return f"{lang}_{stage}_{model_type}"

    def get_ocr_instance(self, lang: str = "ch", stage: str = "baseline", 
                         use_fast_models: bool = False, **other_params):
        """获取OCR实例，支持两阶段检测和性能优化
        
        Args:
            lang: 语言代码
            stage: 检测阶段 (baseline 或 balanced_v1)
            use_fast_models: 是否使用快速模型
            **other_params: 其他参数
        """
        cache_key = self._generate_cache_key(lang, stage, use_fast_models)

        with self._cache_lock:
            # 检查缓存中是否存在
            if cache_key in self._cache:
                self._usage_stats[cache_key] = time.time()
                logger.debug(f"从缓存中获取OCR实例: {cache_key}")
                return self._cache[cache_key]

            # 缓存中不存在，创建新实例
            logger.info(f"创建新的OCR实例并缓存: {cache_key}")

            # 如果缓存已满，使用LRU策略淘汰最久未使用的实例
            if len(self._cache) >= self._max_cache_size:
                self._evict_lru_instance()

            # 创建新的OCR实例
            ocr_instance = self._create_ocr_instance(
                lang, stage, use_fast_models, **other_params
            )

            # 添加到缓存
            self._cache[cache_key] = ocr_instance
            self._usage_stats[cache_key] = time.time()

            return ocr_instance

    def _evict_lru_instance(self):
        """淘汰最久未使用的OCR实例"""
        if not self._usage_stats:
            return

        # 找到最久未使用的实例
        lru_key = min(self._usage_stats, key=self._usage_stats.get)

        # 删除缓存
        if lru_key in self._cache:
            del self._cache[lru_key]
        del self._usage_stats[lru_key]

        logger.info(f"淘汰LRU OCR实例: {lru_key}")

    def _create_ocr_instance(self, lang: str, stage: str, use_fast_models: bool, **other_params):
        """创建新的OCR实例，集成了调优后的参数配置
        
        Args:
            lang: 语言代码
            stage: 检测阶段 (baseline 或 balanced_v1)
            use_fast_models: 是否使用快速模型
        """

        # 强制GPU环境配置 (仅在启用GPU时设置)
        if GPU_ENABLED:
            os.environ["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
            os.environ["CUDA_VISIBLE_DEVICES"] = "0"
            os.environ["FLAGS_use_gpu"] = "true"
            os.environ["FLAGS_fraction_of_gpu_memory_to_use"] = "0.8"
        else:
            # 显式禁用GPU，防止环境变量残留影响
            os.environ["FLAGS_use_gpu"] = "false"
            os.environ["CUDA_VISIBLE_DEVICES"] = ""
            # 关键：防止 CPU 模式下 OpenMP 线程冲突导致死锁
            os.environ["OMP_NUM_THREADS"] = "1" 
            os.environ["KMP_DUPLICATE_LIB_OK"] = "TRUE"
            # 新增：禁用 MKLDNN，防止在某些 CPU 环境下死锁
            os.environ["FLAGS_use_mkldnn"] = "false" 
            os.environ["FLAGS_enable_mkldnn"] = "false"

        # 获取阶段参数配置
        stage_params = PARAM_VERSIONS.get(stage, PARAM_VERSIONS["baseline"])
        
        # 根据性能配置选择模型
        if use_fast_models:
            det_model, rec_model = "PP-OCRv5_mobile_det", "PP-OCRv5_mobile_rec"
        else:
            det_model, rec_model = "PP-OCRv5_server_det", "PP-OCRv5_server_rec"
        
        # 基本参数 - 使用调优后的配置
        init_kwargs = {
            'device': 'gpu' if GPU_ENABLED else 'cpu',
            'lang': lang,
            'use_doc_orientation_classify': stage_params.get('use_doc_orientation_classify', False),
            'use_doc_preprocessor': stage_params.get('use_doc_preprocessor', False),
            'use_doc_unwarping': stage_params.get('use_doc_unwarping', False),
            'use_textline_orientation': stage_params.get('use_textline_orientation', False),
            'text_detection_model_name': det_model,
            'text_recognition_model_name': rec_model,
        }

        # 使用PaddleX创建OCR pipeline，集成调优参数
        device_name = f"gpu:{0}" if GPU_ENABLED else "cpu"
        
        # 确保GPU环境
        # self._ensure_gpu_environment()
        
        try:
            # 尝试启用HPI加速（如果可用）
            pipeline_config = {
                "device": device_name,
                "use_doc_preprocessor": init_kwargs.get('use_doc_preprocessor', False),
                "use_textline_orientation": init_kwargs.get('use_textline_orientation', False),
                "use_doc_orientation_classify": init_kwargs.get('use_doc_orientation_classify', False),
                "use_doc_unwarping": init_kwargs.get('use_doc_unwarping', False),
                "text_detection_model_name": init_kwargs.get('text_detection_model_name'),
                "text_recognition_model_name": init_kwargs.get('text_recognition_model_name'),
            }
            
            logger.info(f"正在创建PaddleX Pipeline (Device={device_name})...")
            
            # 仅在 GPU 模式下尝试 HPI，CPU 模式直接禁用
            if GPU_ENABLED:
                try:
                    pipeline_config["use_hpip"] = True
                    logger.info("尝试启用 HPI 高性能推理...")
                    ocr_instance = create_pipeline("OCR", **pipeline_config)
                    logger.info(f"✅ HPI高性能推理已启用: {stage}_{lang}")
                except Exception as hpi_error:
                    logger.warning(f"⚠️  HPI不可用，回退到标准推理: {hpi_error}")
                    pipeline_config["use_hpip"] = False
                    ocr_instance = create_pipeline("OCR", **pipeline_config)
            else:
                # CPU 模式直接使用标准推理
                pipeline_config["use_hpip"] = False
                logger.info("CPU模式: 使用标准推理创建 Pipeline...")
                ocr_instance = create_pipeline("OCR", **pipeline_config)

            logger.info(f"✅ 成功创建PaddleX OCR实例: {stage}_{lang}_{det_model.split('_')[-2]}")
            return ocr_instance
        except Exception as e:
            # 不再回退到PaddleOCR，直接抛出异常终止
            logger.error(f"创建PaddleX实例失败: {e}")
            raise RuntimeError(f"PaddleX OCR实例创建失败: {e}") from e

    def _ensure_gpu_environment(self):
        """确保GPU环境正确配置"""
        if not GPU_ENABLED:
            return
            
        try:
            if not paddle.device.is_compiled_with_cuda():
                raise EnvironmentError("未启用GPU，请安装GPU版PaddlePaddle")
            if paddle.device.cuda.device_count() <= 0:
                raise EnvironmentError("未检测到可用GPU，请检查驱动与CUDA环境")
            
            # 设置GPU设备
            paddle.device.set_device("gpu:0")
            logger.debug("GPU环境检查通过")
        except Exception as e:
            logger.error(f"GPU环境检查失败: {e}")
            raise

    def warmup_pipeline(self, pipeline, warmup_image_path: str = None):
        """
        预热GPU和Pipeline，提升后续处理速度
        
        Args:
            pipeline: OCR pipeline实例
            warmup_image_path: 预热用的图片路径
        """
        if warmup_image_path and Path(warmup_image_path).exists():
            try:
                warmup_start = time.time()
                
                # 推理预热：一次前向传播完成CUDA初始化和模型加载
                results = list(pipeline.predict([warmup_image_path]))
                
                # 清理结果释放内存
                del results
                
                warmup_time = time.time() - warmup_start
                logger.debug(f"GPU预热完成 (耗时: {warmup_time:.2f}秒)")
                
            except Exception as e:
                logger.warning(f"GPU预热失败: {e}")
        else:
            logger.debug("未找到预热图片，跳过GPU预热")

    def clear_cache(self):
        """清空缓存"""
        with self._cache_lock:
            self._cache.clear()
            self._usage_stats.clear()
            logger.info("OCR实例池缓存已清空")

    def get_cache_info(self) -> Dict:
        """获取缓存信息"""
        with self._cache_lock:
            return {
                'cached_instances': len(self._cache),
                'max_cache_size': self._max_cache_size,
                'cache_keys': list(self._cache.keys())
            }


class OCRService:

    _ocr_instance = None
    _init_lock = threading.Lock()
    # 添加日志参数输出标记，用于控制公共参数只输出一次
    _common_params_logged = False
    _common_params_task_id = None

    def __init__(self, lang: str = "ch", id: Optional[str] = None):
        """
        初始化OCR服务
        
        Args:
            lang: 识别语言，默认为中文
            id: OCR任务ID
        """
        self.lang = lang
        self.id = id

        # 初始化OCR实例池
        self.ocr_pool = OCRInstancePool()
        

    def _collect_texts_from_json(self, data: Any) -> List[str]:

        texts: List[str] = []

        def _walk(node: Any):
            if isinstance(node, dict):
                for key, value in node.items():
                    key_lower = str(key).lower()
                    if key_lower in {"text", "transcription", "label", "text_content"}:
                        if isinstance(value, str) and value:
                            texts.append(value)
                    _walk(value)
            elif isinstance(node, list):
                for item in node:
                    _walk(item)

        _walk(data)
        seen = set()
        unique_texts: List[str] = []
        for t in texts:
            if t not in seen:
                seen.add(t)
                unique_texts.append(t)
        return unique_texts

    def _load_image_unicode(self, abs_path: str) -> Optional[np.ndarray]:
 
        try:
            data = np.fromfile(abs_path, dtype=np.uint8)
            if data is None or data.size == 0:
                return None
            # 通过封装的 imdecode 调用
            imread_color = getattr(cv2, 'IMREAD_COLOR', 1)
            img = self._cv_imdecode(data, imread_color)
            return img
        except Exception:
            return None

    def _cv_polylines(self, img: np.ndarray, pts: np.ndarray,
                       closed: bool, color: tuple, thickness: int) -> None:
        """包装 cv2.polylines，避免静态分析器告警。"""
        try:
            fn = getattr(cv2, 'polylines', None)
            if fn is not None:
                fn(img, [pts], closed, color, thickness)
        except Exception:
            pass

    def _cv_put_text(self, img: np.ndarray, text: str, org: tuple,
                      font: int, font_scale: float,
                      color: tuple, thickness: int, line_type: int) -> None:
        """包装 cv2.putText，避免静态分析器告警。"""
        try:
            fn = getattr(cv2, 'putText', None)
            if fn is not None:
                fn(img, text, org, font, font_scale, color, thickness, line_type)
        except Exception:
            pass

    def _cv_imwrite(self, path: str, img: np.ndarray) -> None:
        """包装 cv2.imwrite，避免静态分析器告警。"""
        try:
            fn = getattr(cv2, 'imwrite', None)
            if fn is not None:
                fn(path, img)
        except Exception:
            pass

    def _draw_visualization(self,
                            img: np.ndarray,
                            items: List[Tuple[Any, Any, Any]],
                            out_path: str,
                            annotate: bool = False) -> None:
        """绘制识别可视化结果并保存到指定路径。

        Args:
            img: 原始图像数组
            items: 三元组列表 (poly, text, score)
            out_path: 输出文件完整路径
            annotate: 是否在图上标注文本与分数
        """
        try:
            vis = img.copy()
            for (poly, t, s) in items:
                try:
                    pts = np.array(poly, dtype=np.int32)
                    if pts.ndim == 2 and pts.shape[0] >= 4:
                        self._cv_polylines(vis, pts, True, (0, 255, 0), 2)
                        x = int(np.min(pts[:, 0]))
                        y = int(np.min(pts[:, 1]))
                    else:
                        x, y = 2, 20
                    if annotate:
                        label = f"{t} ({float(s):.2f})"
                        font = getattr(cv2, 'FONT_HERSHEY_SIMPLEX', 0)
                        line_aa = getattr(cv2, 'LINE_AA', 16)
                        self._cv_put_text(
                            vis, label, (x, max(0, y - 5)),
                            font, 0.5, (0, 0, 255), 1, line_aa
                        )
                except Exception:
                    continue
            try:
                os.makedirs(os.path.dirname(out_path), exist_ok=True)
            except Exception:
                pass
            self._cv_imwrite(out_path, vis)
        except Exception:
            pass

    def _cv_imdecode(self, data: np.ndarray, flags: int) -> Optional[np.ndarray]:
        """包装 cv2.imdecode。"""
        try:
            fn = getattr(cv2, 'imdecode', None)
            if fn is None or not callable(fn):
                return None
            return fn(data, flags)
        except Exception:
            return None

    def _cv_resize(self, img: np.ndarray, size: tuple) -> np.ndarray:
        """包装 cv2.resize。"""
        try:
            fn = getattr(cv2, 'resize', None)
            if fn is None or not callable(fn):
                return img
            return fn(img, size)
        except Exception:
            return img

    def _log_effective_round_params(self, ocr_obj: PaddleOCR, rp: Dict[str, Any], ridx: int) -> None:
        """打印OCR实例内部真正生效的关键参数，用于核对是否与本轮参数一致。
        仅日志，不抛异常。"""
        try:
            eff = {
                'det_limit_type': None,
                'det_limit_side_len': None,
                'det_thresh': None,
                'det_box_thresh': None,
                'det_unclip_ratio': None,
                'rec_score_thresh': None,
            }
            det = getattr(ocr_obj, 'text_detector', None)
            if det is not None:
                pre = getattr(det, 'preprocess_op', None)
                if pre is not None:
                    eff['det_limit_type'] = getattr(pre, 'limit_type', None)
                    eff['det_limit_side_len'] = getattr(pre, 'limit_side_len', None)
                post = getattr(det, 'postprocess_op', None)
                if post is not None:
                    eff['det_thresh'] = getattr(post, 'thresh', None)
                    eff['det_box_thresh'] = getattr(post, 'box_thresh', None)
                    eff['det_unclip_ratio'] = getattr(post, 'unclip_ratio', None)
            rec = getattr(ocr_obj, 'text_recognizer', None)
            if rec is not None:
                post = getattr(rec, 'postprocess_op', None)
                if post is not None:
                    eff['rec_score_thresh'] = getattr(post, 'score_thresh', None)
            logger.warning(
                "参数生效校验|轮次=%s | 期望: lt=%s side=%s det=%.2f box=%.2f unclip=%.2f rec=%.2f | 实际: lt=%s side=%s det=%s box=%s unclip=%s rec=%s",
                ridx,
                rp.get('text_det_limit_type'), rp.get('text_det_limit_side_len'),
                float(rp.get('text_det_thresh')), float(rp.get('text_det_box_thresh')),
                float(rp.get('text_det_unclip_ratio')), float(rp.get('text_rec_score_thresh')),
                eff['det_limit_type'], eff['det_limit_side_len'], eff['det_thresh'],
                eff['det_box_thresh'], eff['det_unclip_ratio'], eff['rec_score_thresh']
            )
        except Exception:
            pass

    def detect_language(self, text: str) -> Dict:
        """
        检测文本语言

        Args:
            text: 待检测文本

        Returns:
            Dict: 语言检测结果
        """
        # 语言检测规则
        languages = {
            'chinese': False,
            'english': False,
            'vietnamese': False,
            'korean': False,
            'japanese': False,
        }

        # 中文字符范围（包括简体和繁体）
        # CJK统一汉字 (U+4E00-U+9FFF)
        # CJK统一汉字扩展A (U+3400-U+4DBF)
        # CJK统一汉字扩展B (U+20000-U+2A6DF)
        # CJK部首扩展 (U+2E80-U+2EFF)
        # CJK笔画 (U+31C0-U+31EF)
        # CJK符号和标点 (U+3000-U+303F)
        if (any('\u4e00' <= ch <= '\u9fff' for ch in text) or  # 基本汉字
            any('\u3400' <= ch <= '\u4dbf' for ch in text) or  # 扩展A
            any('\u2e80' <= ch <= '\u2eff' for ch in text) or  # 部首扩展
            any('\u31c0' <= ch <= '\u31ef' for ch in text) or  # 笔画
            any('\u3000' <= ch <= '\u303f' for ch in text) or  # 符号和标点
            any(ord(ch) >= 0x20000 and ord(ch) <= 0x2a6df for ch in text)):  # 扩展B
            languages['chinese'] = True

        # 英文字符
        if re.search(r'[a-zA-Z]{2,}', text):
            languages['english'] = True

        # 越南文检测 (特殊字符)
        vietnamese_chars = 'ăâêôơưđ'
        vietnamese_chars += vietnamese_chars.upper()
        vietnamese_marks = '\u0300\u0301\u0303\u0309\u0323'  # 声调符号
        if any(ch in vietnamese_chars for ch in text) or any(ch in vietnamese_marks for ch in text):
            languages['vietnamese'] = True

        # 韩文字符范围
        if any('\uac00' <= ch <= '\ud7a3' for ch in text):
            languages['korean'] = True

        # 日文字符范围 (平假名和片假名)
        if any('\u3040' <= ch <= '\u30ff' for ch in text) or any('\u31f0' <= ch <= '\u31ff' for ch in text):
            languages['japanese'] = True

        return languages

    @staticmethod
    def check_language_match(texts: List[str], target_language: str) -> bool:
        """
        检查文本是否包含目标语言

        Args:
            texts: 文本列表
            target_language: 目标语言代码（支持官方与内部代码）

        Returns:
            bool: 是否包含目标语言
        """
        # 如果没有文本，直接返回False
        if not texts:
            return False

        # 语言代码映射（按照PaddleOCR官方文档标准）
        language_patterns = {
            'ch': r'[\u4e00-\u9fff]',           # 中文（官方标准）
            'en': r'[a-zA-Z]',                  # 英文
            'korean': r'[\uac00-\ud7a3]',       # 韩文（官方标准）
            'ko': r'[\uac00-\ud7a3]',           # 韩文（兼容）
            'japan': r'[\u3040-\u30ff]',        # 日文（官方标准）
            'ja': r'[\u3040-\u30ff]',           # 日文（兼容）
            'chinese_cht': r'[\u4e00-\u9fff]',  # 繁体中文
            'te': r'[\u0c00-\u0c7f]',           # 泰卢固文
            'ka': r'[\u0c80-\u0cff]',           # 卡纳达文
            'ta': r'[\u0b80-\u0bff]',           # 泰米尔文
        }

        # 获取目标语言的正则表达式
        pattern = language_patterns.get(target_language.lower())
        if not pattern:
            # 如果目标语言不在预定义列表中，默认为中文
            pattern = language_patterns['zh']

        # 编译正则表达式
        regex = re.compile(pattern)

        # 检查每个文本是否包含目标语言
        for text in texts:
            if regex.search(text):
                return True

        return False


    @staticmethod
    def _text_is_chinese_only(text: str) -> bool:
        """仅保留包含中文且不含英文字母和数字的文本。"""
        if not text:
            return False
        has_cn = any('\u4e00' <= ch <= '\u9fff' for ch in text)
        has_ascii_alnum = any((ord(ch) < 128) and (ch.isalpha() or ch.isdigit()) for ch in text)
        return has_cn and not has_ascii_alnum

    @staticmethod
    def _text_contains_chinese(text: str) -> bool:
        """判断文本是否包含中文字符。"""
        if not text:
            return False
        return any('\u4e00' <= ch <= '\u9fff' for ch in text)

    def _text_passes_language_filter(self, text: str, strategy: str) -> bool:
        """根据配置的语言过滤策略判断文本是否通过。

        参数:
            text: 待判断文本
            strategy: 过滤策略，支持 chinese_only/contains_chinese/none
        返回:
            bool: 是否通过过滤
        """
        try:
            mode = str(strategy or '').strip().lower()
        except Exception:
            mode = 'chinese_only'
        if mode == 'none':
            return bool(text)
        if mode == 'contains_chinese':
            return self._text_contains_chinese(text)
        # 默认 chinese_only
        return self._text_is_chinese_only(text)

    def _resolve_text_filter_strategy(self) -> str:
        """基于现有语言配置推导文本过滤策略。

        规则：
        - 若当前语言为中文(ch/zh/zh-cn等)，使用 contains_chinese（包含中文即可）
        - 其它语言或未知，返回 none（不过滤，仅用分数阈值）
        说明：不新增配置项，直接沿用 ocr_default_lang/default_language
        """
        try:
            # 优先使用实例语言，其次使用配置
            cur_lang = str(self.lang or '').strip().lower()
            if not cur_lang:
                cfg = settings.CFG._config
                cur_lang = (cfg.get('ocr', 'ocr_default_lang', fallback='')
                            or cfg.get('ocr', 'default_language', fallback=''))
                cur_lang = str(cur_lang or '').strip().lower()
        except Exception:
            cur_lang = 'ch'
        if cur_lang in {'ch', 'zh', 'zh-cn', 'zh_cn', 'chinese'}:
            return 'contains_chinese'
        return 'none'

    @staticmethod
    def _parse_predict_items_paddlex(result_data: Any) -> List[Any]:
        """解析 PaddleX.predict 的返回，输出 (poly, text, score) 列表。
        
        PaddleX返回格式与demo保持一致。"""
        items: List[Any] = []
        if result_data is None:
            return items
        try:
            # PaddleX返回格式：result_data包含texts, scores, boxes等字段
            texts = result_data.get('texts', [])
            scores = result_data.get('scores', [])
            boxes = result_data.get('boxes', [])
            
            if not isinstance(texts, list):
                texts = [texts] if texts else []
            if not isinstance(scores, list):
                scores = [scores] if scores else []
            if not isinstance(boxes, list):
                boxes = [boxes] if boxes else []
                
            n = max(len(texts), len(scores), len(boxes))
            for i in range(n):
                t = texts[i] if i < len(texts) else ""
                try:
                    s = float(scores[i]) if i < len(scores) else 0.0
                except Exception:
                    s = 0.0
                p = boxes[i] if i < len(boxes) else []
                items.append((p, str(t), float(s)))
        except Exception:
            return items
        return items

    @staticmethod
    def _parse_predict_items(res: Any) -> List[Any]:
        """解析 PaddleOCR.predict 的返回，输出 (poly, text, score) 列表。
        
        为简化仅返回三元组，poly 可为空列表。"""
        items: List[Any] = []
        if res is None:
            return items
        try:
            if isinstance(res, list) and res:
                first = res[0]
                if isinstance(first, dict):
                    raw = first
                    texts = raw.get('rec_texts') or raw.get('texts') or []
                    scores = raw.get('rec_scores') or []
                    polys = (raw.get('rec_polys') or raw.get('dt_polys')
                             or raw.get('boxes') or [])
                    if texts and not isinstance(texts, list):
                        texts = [texts]
                    if scores and not isinstance(scores, list):
                        scores = [scores]
                    if polys is None:
                        polys = []
                    n = max(len(texts), len(scores), len(polys))
                    for i in range(n):
                        t = texts[i] if i < len(texts) else ""
                        try:
                            s = float(scores[i]) if i < len(scores) else 0.0
                        except Exception:
                            s = 0.0
                        p = polys[i] if i < len(polys) else []
                        items.append((p, str(t), float(s)))
                    return items
                if isinstance(first, list):
                    for itm in first:
                        if (isinstance(itm, list) and len(itm) >= 2 and
                                isinstance(itm[0], (list, np.ndarray)) and
                                isinstance(itm[1], (list, tuple))):
                            poly = itm[0]
                            text = str(itm[1][0])
                            try:
                                score = float(itm[1][1])
                            except Exception:
                                score = 0.0
                            items.append((poly, text, score))
                    return items
            if isinstance(res, dict):
                raw = res
                texts = raw.get('rec_texts') or raw.get('texts') or []
                scores = raw.get('rec_scores') or []
                polys = (raw.get('rec_polys') or raw.get('dt_polys')
                         or raw.get('boxes') or [])
                if texts and not isinstance(texts, list):
                    texts = [texts]
                if scores and not isinstance(scores, list):
                    scores = [scores]
                if polys is None:
                    polys = []
                n = max(len(texts), len(scores), len(polys))
                for i in range(n):
                    t = texts[i] if i < len(texts) else ""
                    try:
                        s = float(scores[i]) if i < len(scores) else 0.0
                    except Exception:
                        s = 0.0
                    p = polys[i] if i < len(polys) else []
                    items.append((p, str(t), float(s)))
        except Exception:
            return items
        return items

    @staticmethod
    def _compute_scaled_resolution(h: int, w: int, limit_type: str,
                                   side_len: int) -> str:
        """根据 limit_type 与 side_len 估算等比缩放后的分辨率字符串。
        逻辑与 helper 保持一致。"""
        try:
            h = int(h); w = int(w); side = int(side_len)
            if h <= 0 or w <= 0 or side <= 0:
                return f"{w}x{h}"
            lt = str(limit_type)
            cur_max = float(max(h, w))
            cur_min = float(min(h, w))
            if lt == 'max':
                if cur_max > side:
                    r = float(side) / cur_max
                    sw = int(round(w * r)); sh = int(round(h * r))
                    return f"{max(1, sw)}x{max(1, sh)}"
                return f"{w}x{h}"
            if lt == 'min':
                if cur_min < side:
                    r = float(side) / cur_min
                    sw = int(round(w * r)); sh = int(round(h * r))
                    return f"{max(1, sw)}x{max(1, sh)}"
                return f"{w}x{h}"
            denom = float(min(h, w)) if lt == 'min' else float(max(h, w))
            if denom <= 0:
                return f"{w}x{h}"
            r = float(side) / denom
            sw = int(round(w * r)); sh = int(round(h * r))
            return f"{max(1, sw)}x{max(1, sh)}"
        except Exception:
            return f"{w}x{h}"

    @staticmethod
    def _resize_image_for_round(img: np.ndarray, limit_type: str,
                                side_len: int) -> np.ndarray:
        """按轮次参数进行前置等比缩放，确保缩放策略稳定生效。

        说明：不依赖底层引擎的预处理开关，直接对输入图片做等比缩放，
        与 _compute_scaled_resolution 的逻辑保持一致。
        """
        try:
            h, w = img.shape[:2]
            if h <= 0 or w <= 0 or side_len <= 0:
                return img
            lt = str(limit_type)
            cur_max = float(max(h, w))
            cur_min = float(min(h, w))
            if lt == 'max':
                if cur_max <= side_len:
                    return img
                r = float(side_len) / cur_max
            else:
                if cur_min >= side_len:
                    return img
                r = float(side_len) / cur_min
            new_w = max(1, int(round(w * r)))
            new_h = max(1, int(round(h * r)))
            # 使用封装的 resize
            return OCRService._cv_resize(OCRService, img, (new_w, new_h))
        except Exception:
            return img

    @staticmethod
    def _validate_round_params(p: Dict[str, Any]) -> Dict[str, Any]:
        """校验并规范化每轮参数，仅接受官方字段名。"""
        required = [
            'text_det_limit_type',
            'text_det_limit_side_len',
            'text_det_thresh',
            'text_det_box_thresh',
            'text_det_unclip_ratio',
            'text_rec_score_thresh',
        ]
        for k in required:
            if k not in p or p.get(k) is None or str(p.get(k)).strip() == '':
                raise ValueError(f"缺少必要参数: {k}")
        out: Dict[str, Any] = dict(p)
        out['text_det_limit_type'] = str(p.get('text_det_limit_type'))
        out['text_det_limit_side_len'] = int(float(p.get('text_det_limit_side_len')))
        out['text_det_thresh'] = float(p.get('text_det_thresh'))
        out['text_det_box_thresh'] = float(p.get('text_det_box_thresh'))
        out['text_det_unclip_ratio'] = float(p.get('text_det_unclip_ratio'))
        out['text_rec_score_thresh'] = float(p.get('text_rec_score_thresh'))
        out['use_doc_unwarping'] = bool(p.get('use_doc_unwarping', False))
        out['use_textline_orientation'] = bool(
            p.get('use_textline_orientation', False)
        )
        return out

    @staticmethod
    def _default_round_param_sets() -> List[Dict[str, Any]]:
        """从performance_config读取两阶段参数配置"""
        from .performance_config import PARAM_VERSIONS
        return [
            PARAM_VERSIONS.get('baseline', {}),
            PARAM_VERSIONS.get('balanced_v1', {}),
        ]

    def _load_rounds_from_config(self) -> List[Dict[str, Any]]:
        """从配置加载多轮参数，支持两种方式：
        1) [ocr_rounds] section: round1=..., round2=...（JSON字符串）
        2) [ocr] rounds_json=... （JSON数组字符串）
        若都不存在，返回默认集。
        """
        try:
            cfg = settings.CFG._config
            # 方式2：ocr.rounds_json
            try:
                rounds_json = cfg.get('ocr', 'rounds_json', fallback='').strip()
            except Exception:
                rounds_json = ''
            if rounds_json:
                arr = json.loads(rounds_json)
                if isinstance(arr, list) and arr:
                    return [self._validate_round_params(x) for x in arr]
            # 方式1：独立 section
            if cfg.has_section('ocr_rounds'):
                parser: configparser.SectionProxy = cfg['ocr_rounds']
                collected: List[Dict[str, Any]] = []
                for key in sorted(parser.keys()):
                    try:
                        val = parser.get(key)
                        obj = json.loads(val)
                        if isinstance(obj, dict):
                            collected.append(self._validate_round_params(obj))
                    except Exception:
                        continue
                if collected:
                    return collected
        except Exception as e:
            logger.warning(f"读取多轮参数失败，使用默认集: {e}")
        # 默认
        return [self._validate_round_params(x)
                for x in self._default_round_param_sets()]

    @staticmethod
    def _force_apply_params_to_instance(ocr_obj: PaddleOCR,
                                        params: Dict[str, Any]) -> None:
        """将 text_* 阈值与预处理策略强制写入已创建的 OCR 实例。"""
        try:
            det = getattr(ocr_obj, 'text_detector', None)
            if det and hasattr(det, 'postprocess_op'):
                post = det.postprocess_op
                if hasattr(post, 'thresh'):
                    setattr(post, 'thresh', float(params['text_det_thresh']))
                if hasattr(post, 'box_thresh'):
                    setattr(post, 'box_thresh', float(params['text_det_box_thresh']))
                if hasattr(post, 'unclip_ratio'):
                    setattr(post, 'unclip_ratio', float(params['text_det_unclip_ratio']))
            if det and hasattr(det, 'preprocess_op'):
                pre = det.preprocess_op
                if hasattr(pre, 'limit_type'):
                    setattr(pre, 'limit_type', str(params['text_det_limit_type']))
                if hasattr(pre, 'limit_side_len'):
                    setattr(pre, 'limit_side_len', int(params['text_det_limit_side_len']))
            rec = getattr(ocr_obj, 'text_recognizer', None)
            if rec and hasattr(rec, 'postprocess_op'):
                post = rec.postprocess_op
                if hasattr(post, 'score_thresh'):
                    setattr(post, 'score_thresh', float(params['text_rec_score_thresh']))
        except Exception as e:
            logger.debug(f"强制写入参数失败(忽略)：{e}")

    def _get_ocr_for_round(self, params: Dict[str, Any]) -> PaddleOCR:
        """依据轮次参数获取/创建 OCR 实例，并强制写入阈值。"""
        use_textline_orientation = bool(params.get('use_textline_orientation', False))
        ocr_inst = self.ocr_pool.get_ocr_instance(
            lang=self.lang,
            limit_type=str(params['text_det_limit_type']),
            text_det_thresh=float(params['text_det_thresh']),
            text_det_box_thresh=float(params['text_det_box_thresh']),
            text_det_unclip_ratio=float(params['text_det_unclip_ratio']),
            text_det_limit_side_len=int(params['text_det_limit_side_len']),
            text_rec_score_thresh=float(params['text_rec_score_thresh']),
            use_textline_orientation=use_textline_orientation,
            use_doc_unwarping=bool(params.get('use_doc_unwarping', False))
        )
        self._force_apply_params_to_instance(ocr_inst, params)
        return ocr_inst

    @staticmethod
    def calculate_image_hash(image_path, include_path=True) -> str:
        """计算图片的MD5哈希值，用于唯一标识图片内容。"""
        hash_md5 = hashlib.md5()
        try:
            with open(image_path, "rb") as f:
                for chunk in iter(lambda: f.read(4096), b""):
                    hash_md5.update(chunk)
            img_md5 = hash_md5.hexdigest()
            if not include_path:
                return img_md5
            # 包含路径的哈希
            image_path = image_path.replace('\\', '/')
            combined = f"{image_path}|{img_md5}"
            combined_hash = hashlib.md5(combined.encode('utf-8')).hexdigest()
            return combined_hash
        except Exception:
            return ""


    @staticmethod
    def _round_signature(p: Dict[str, Any]) -> str:
        """基于轮次参数生成稳定签名，用于报表与日志对齐实际轮次。
        仅包含会影响识别口径的关键字段。
        """
        try:
            lt = str(p.get('text_det_limit_type'))
            side = int(float(p.get('text_det_limit_side_len')))
            det = float(p.get('text_det_thresh'))
            box = float(p.get('text_det_box_thresh'))
            unclip = float(p.get('text_det_unclip_ratio'))
            rec = float(p.get('text_rec_score_thresh'))
            unwarp = 1 if bool(p.get('use_doc_unwarping', False)) else 0
            textline = 1 if bool(p.get('use_textline_orientation', False)) else 0
            return (
                f"lt={lt}|side={side}|det={det:.2f}|box={box:.2f}|"
                f"unclip={unclip:.2f}|rec={rec:.2f}|unwarp={unwarp}|textline={textline}"
            )
        except Exception:
            return "lt=?|side=?|det=?|box=?|unclip=?|rec=?|unwarp=?|textline=?"

    def _contains_chinese(self, text: str) -> bool:
        """检查文本是否包含中文字符"""
        import re
        chinese_pattern = re.compile(r"[\u4e00-\u9fff]")
        return bool(chinese_pattern.search(text))

    def recognize_rounds_batch(self,
                               image_dir: Union[str, List[str]],
                               image_formats: Optional[List[str]] = None,
                               enable_draw: Optional[bool] = None,
                               enable_copy: Optional[bool] = None,
                               enable_annotate: Optional[bool] = None
                               ) -> Dict[str, Any]:
        """使用PaddleX产线的两阶段OCR识别：baseline + balanced_v1"""
        
        # 获取图片路径列表
        if type(image_dir) is str:
            img_exts = image_formats or ['.jpg', '.jpeg', '.png']
            full_image_dir = image_dir
            if not os.path.isabs(image_dir):
                full_image_dir = os.path.join(settings.MEDIA_ROOT, image_dir)
            if not os.path.exists(full_image_dir) or not os.path.isdir(full_image_dir):
                return {"error": f"图片目录不存在或不是目录: {full_image_dir}"}

            image_paths: List[str] = []
            for root_dir, _, files in os.walk(full_image_dir):
                for fname in files:
                    if any(fname.lower().endswith(ext) for ext in img_exts):
                        image_paths.append(os.path.join(root_dir, fname))
        elif type(image_dir) is list:
            image_paths = image_dir
        else:
            return {"error": "image_dir 参数类型错误"}

        if not image_paths:
            return {"error": "未找到图片文件"}

        total_images = len(image_paths)
        logger.info(f"开始PaddleX两阶段OCR识别，总图片数: {total_images}")

        # 从performance_config读取两阶段参数配置
        from .performance_config import PARAM_VERSIONS
        param_configs = [
            PARAM_VERSIONS.get('baseline', {}),
            PARAM_VERSIONS.get('balanced_v1', {}),
        ]

        # 统计结果
        total_hit = 0
        first_hit_info: Dict[str, Dict[str, Any]] = {}
        final_miss_paths: List[str] = []
        total_processed = 0
        
        # 阶段1处理的图片列表
        stage1_miss_paths = []

        # 阶段1: baseline检测
        logger.info("开始阶段1: baseline检测")
        for p in image_paths:
            total_processed += 1
            
            # 更新进度
            notify_ocr_task_progress({
                "id": self.id,
                "processed_images": total_processed,
                "remark": f"阶段1识别第 {total_processed} 张 / 共 {total_images} 张",
            }, debounce=True)
            
            try:
                img = self._load_image_unicode(p)
                if img is None:
                    stage1_miss_paths.append(p)
                    continue
            except Exception:
                stage1_miss_paths.append(p)
                continue

            fname = os.path.basename(p)
            hit_in_stage1 = False
            
            # 尝试baseline配置
            try:
                config = param_configs[0]  # baseline
                logger.info(f"图片 {fname} 开始阶段1处理，配置: {config}")
                
                # 获取OCR实例
                try:
                    ocr_inst = self._get_ocr_for_round(config)
                    logger.info(f"图片 {fname} OCR实例获取成功")
                except Exception as ocr_e:
                    logger.error(f"图片 {fname} OCR实例获取失败: {ocr_e}")
                    raise ocr_e
                
                # 使用PaddleX API进行预测
                try:
                    results = list(ocr_inst.predict([img], **config))
                    logger.info(f"图片 {fname} 阶段1预测结果数量: {len(results)}")
                except Exception as pred_e:
                    logger.error(f"图片 {fname} 阶段1预测失败: {pred_e}")
                    raise pred_e
                
                if results:
                    result = results[0]
                    result_data = result.json["res"]
                    
                    # 检查是否包含中文文本
                    rec_texts = result_data.get("rec_texts", [])
                    logger.debug(f"图片 {fname} 阶段1识别到的文本: {rec_texts}")
                    
                    # 检查每个文本是否包含中文
                    chinese_texts = []
                    for text in rec_texts:
                        if self._contains_chinese(text):
                            chinese_texts.append(text)
                    
                    logger.debug(f"图片 {fname} 阶段1中文文本: {chinese_texts}")
                    
                    if rec_texts and any(self._contains_chinese(text) for text in rec_texts):
                        # 阶段1命中
                        hit_in_stage1 = True
                        total_hit += 1
                        
                        # 解析结果
                        items = self._parse_predict_items_paddlex(result_data)
                        scores = [float(s) for (poly, t, s) in items] if items else [1.0]
                        
                        first_hit_info[fname] = {
                            "round": 1,
                            "hit_texts": "|".join(rec_texts[:10]),
                            "hit_scores": "|".join([f"{s:.4f}" for s in scores[:10]])
                        }
                        
                        logger.info(f"图片 {fname} 在阶段1(baseline)中识别成功，中文文本数: {len(chinese_texts)}")
                    else:
                        logger.debug(f"图片 {fname} 阶段1未检测到中文文本")
                        
            except Exception as e:
                logger.debug(f"图片 {fname} 阶段1识别失败: {e}")
            
            # 如果阶段1未命中，加入阶段2处理列表
            if not hit_in_stage1:
                stage1_miss_paths.append(p)

        # 阶段2: balanced_v1检测阶段1未命中的图片
        logger.info(f"开始阶段2: balanced_v1检测，处理 {len(stage1_miss_paths)} 张未命中图片")
        stage2_processed = 0
        
        for p in stage1_miss_paths:
            stage2_processed += 1
            
            # 更新进度
            notify_ocr_task_progress({
                "id": self.id,
                "processed_images": total_processed - len(stage1_miss_paths) + stage2_processed,
                "remark": f"阶段2识别第 {stage2_processed} 张 / 共 {len(stage1_miss_paths)} 张",
            }, debounce=True)
            
            try:
                img = self._load_image_unicode(p)
                if img is None:
                    final_miss_paths.append(p)
                    continue
            except Exception:
                final_miss_paths.append(p)
                continue

            fname = os.path.basename(p)
            hit_in_stage2 = False
            
            # 尝试balanced_v1配置
            try:
                config = param_configs[1]  # balanced_v1
                
                # 获取OCR实例
                ocr_inst = self._get_ocr_for_round(config)
                
                # 使用PaddleX API进行预测
                results = list(ocr_inst.predict([img], **config))
                if results:
                    result = results[0]
                    result_data = result.json["res"]
                    
                    # 检查是否包含中文文本
                    rec_texts = result_data.get("rec_texts", [])
                    if rec_texts and any(self._contains_chinese(text) for text in rec_texts):
                        # 阶段2命中
                        hit_in_stage2 = True
                        total_hit += 1
                        
                        # 解析结果
                        items = self._parse_predict_items_paddlex(result_data)
                        scores = [float(s) for (poly, t, s) in items] if items else [1.0]
                        
                        first_hit_info[fname] = {
                            "round": 2,
                            "hit_texts": "|".join(rec_texts[:10]),
                            "hit_scores": "|".join([f"{s:.4f}" for s in scores[:10]])
                        }
                        
                        logger.debug(f"图片 {fname} 在阶段2(balanced_v1)中识别成功")
                        
            except Exception as e:
                logger.debug(f"图片 {fname} 阶段2识别失败: {e}")
            
            # 如果阶段2也未命中，加入最终未命中列表
            if not hit_in_stage2:
                final_miss_paths.append(p)

        # 返回结果统计
        final_miss = len(final_miss_paths)
        stage1_hits = sum(1 for info in first_hit_info.values() if info["round"] == 1)
        stage2_hits = sum(1 for info in first_hit_info.values() if info["round"] == 2)
        
        logger.info(f"PaddleX两阶段识别完成: 总图片={total_images}, 阶段1命中={stage1_hits}, 阶段2命中={stage2_hits}, 总命中={total_hit}, 未命中={final_miss}")
        
        return {
            "total_hit": total_hit,
            "final_miss": final_miss,
            "first_hit_info": first_hit_info,
            "rounds_root": "",  # 简化版本不生成可视化文件
        }

    def recognize_simple_batch(self,
                               image_dir: Union[str, List[str]],
                               image_formats: Optional[List[str]] = None,
                               enable_draw: Optional[bool] = None,
                               enable_copy: Optional[bool] = None,
                               enable_annotate: Optional[bool] = None
                               ) -> Dict[str, Any]:
        """简化的OCR识别方法：只使用PaddleX产线+两套配置参数"""
        
        # 获取图片路径列表
        if type(image_dir) is str:
            img_exts = image_formats or ['.jpg', '.jpeg', '.png', '.bmp', '.webp', '.tif', '.tiff']
            full_image_dir = image_dir
            if not os.path.isabs(image_dir):
                full_image_dir = os.path.join(settings.MEDIA_ROOT, image_dir)
            if not os.path.exists(full_image_dir) or not os.path.isdir(full_image_dir):
                return {"error": f"图片目录不存在或不是目录: {full_image_dir}"}

            image_paths: List[str] = []
            for root_dir, _, files in os.walk(full_image_dir):
                for fname in files:
                    if any(fname.lower().endswith(ext) for ext in img_exts):
                        image_paths.append(os.path.join(root_dir, fname))
        elif type(image_dir) is list:
            image_paths = image_dir
        else:
            return {"error": "image_dir 参数类型错误"}

        if not image_paths:
            return {"error": "未找到图片文件"}

        total_images = len(image_paths)
        logger.info(f"开始简化OCR识别，总图片数: {total_images}")

        # 简化的两套配置参数
        configs = [
            {
                "det_limit_side_len": 960,
                "det_limit_type": "max",
                "rec_batch_num": 6,
                "use_angle_cls": True,
                "use_space_char": True
            },
            {
                "det_limit_side_len": 1280,
                "det_limit_type": "max", 
                "rec_batch_num": 8,
                "use_angle_cls": False,
                "use_space_char": True
            }
        ]

        # 统计结果
        total_hit = 0
        first_hit_info: Dict[str, Dict[str, Any]] = {}
        final_miss_paths: List[str] = []
        total_processed = 0

        # 逐图片处理
        for p in image_paths:
            total_processed += 1
            
            # 更新进度
            notify_ocr_task_progress({
                "id": self.id,
                "processed_images": total_processed,
                "remark": f"正在识别第 {total_processed} 张 / 共 {total_images} 张",
            }, debounce=True)
            
            try:
                img = self._load_image_unicode(p)
                if img is None:
                    final_miss_paths.append(p)
                    continue
            except Exception:
                final_miss_paths.append(p)
                continue

            fname = os.path.basename(p)
            hit_round = 0
            
            # 尝试两套配置
            for config_idx, config in enumerate(configs, start=1):
                try:
                    # 使用PaddleX产线进行OCR识别
                    ocr_result = self.ocr(img, **config)
                    
                    if ocr_result and len(ocr_result) > 0:
                        # 提取文本
                        texts = [line[1][0] if len(line) > 1 and len(line[1]) > 0 else "" 
                                for line in ocr_result]
                        texts = [t for t in texts if t.strip()]
                        
                        if texts:
                            # 记录命中信息
                            hit_round = config_idx
                            total_hit += 1
                            
                            first_hit_info[fname] = {
                                "round": hit_round,
                                "hit_texts": "|".join(texts[:10]),
                                "hit_scores": "|".join(["1.0000"] * min(len(texts), 10))
                            }
                            
                            logger.debug(f"图片 {fname} 在第{hit_round}轮配置中识别成功，文本数量: {len(texts)}")
                            break
                            
                except Exception as e:
                    logger.debug(f"图片 {fname} 第{config_idx}轮配置识别失败: {e}")
                    continue
            
            # 如果所有配置都失败，记录为未命中
            if hit_round == 0:
                final_miss_paths.append(p)

        # 返回结果统计
        final_miss = len(final_miss_paths)
        logger.info(f"简化OCR识别完成: 总图片={total_images}, 命中={total_hit}, 未命中={final_miss}")
        
        return {
            "total_hit": total_hit,
            "final_miss": final_miss,
            "first_hit_info": first_hit_info,
            "rounds_root": "",  # 简化版本不生成可视化文件
        }


# 如果作为独立脚本运行，提供简单的测试功能
if __name__ == "__main__":
    # 该模块仅作为服务被调用，不提供命令行入口
    print("OCRService 模块：作为服务组件使用，无命令行入口。")
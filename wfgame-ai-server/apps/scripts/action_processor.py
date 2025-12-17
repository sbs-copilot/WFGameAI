# -*- coding: utf-8 -*-
"""
Action处理器模块
负责处理JSON脚本中的各种action操作
"""

# 🔧 新增：禁用第三方库DEBUG日志
import logging

from rest_framework.decorators import action

from utils.socketio_helper import SocketIOHttpApiClient
from utils.socketIo_room_names import device_room

logging.getLogger('airtest').setLevel(logging.WARNING)
logging.getLogger('airtest.core.android.adb').setLevel(logging.WARNING)
import os
import sys
import json
import time
import subprocess
import tempfile
import traceback
import cv2
import numpy as np
import base64
import io
from collections import namedtuple
from app_lifecycle_manager import AppLifecycleManager

# Import try_log_screen function for thumbnail generation
try:
    from replay_script import try_log_screen
except ImportError:
    try_log_screen = None

# Import the screenshot helper function
def get_device_screenshot(device):
    """
    获取设备截图的通用方法，兼容 adbutils.AdbDevice 和 Mock设备

    Args:
        device: adbutils.AdbDevice 对象或 Mock设备

    Returns:
        PIL.Image 对象或 None
    """
    # 仅使用 device_<pk> 房间；禁止使用序列号房间
    room_id = "device_unknown"
    try:
        pk = getattr(device, 'primary_key_id', None) or getattr(device, 'id', None)
        if not pk and hasattr(device, 'serial'):
            # 尝试通过序列号查询 Django Device 模型获取 pk
            try:
                from apps.devices.models import Device as _DModel
                dev_obj = _DModel.objects.filter(device_id=getattr(device, 'serial')).only('id').first()
                if dev_obj:
                    pk = dev_obj.id
            except Exception:
                pk = None
        if pk:
            room_id = f"device_{pk}"
    except Exception:
        room_id = "device_unknown"
    try:
        # 首先检查设备是否有直接的screenshot方法（Mock设备或其他设备类型）
        if hasattr(device, 'screenshot') and callable(device.screenshot):
            screenshot = device.screenshot()
            if screenshot is not None:
                # 统一转换为 base64 字符串
                pic_b64 = None
                try:
                    # PIL.Image
                    from PIL import Image
                    if isinstance(screenshot, Image.Image):
                        buf = io.BytesIO()
                        screenshot.save(buf, format='PNG')
                        pic_b64 = base64.b64encode(buf.getvalue()).decode('utf-8')
                    # bytes/bytearray
                    elif isinstance(screenshot, (bytes, bytearray)):
                        pic_b64 = base64.b64encode(bytes(screenshot)).decode('utf-8')
                    # numpy array (OpenCV)
                    elif isinstance(screenshot, np.ndarray):
                        ok, enc = cv2.imencode('.png', screenshot)
                        if ok:
                            pic_b64 = base64.b64encode(enc.tobytes()).decode('utf-8')
                    # data URL string or raw b64 string
                    elif isinstance(screenshot, str):
                        # 去掉 data:image/*;base64, 前缀（如有）
                        if screenshot.startswith('data:image') and ','.find(screenshot) >= 0:
                            pic_b64 = screenshot.split(',', 1)[1]
                        else:
                            pic_b64 = screenshot
                except Exception as _:
                    pic_b64 = None

                if pic_b64:
                    try:
                        SocketIOHttpApiClient().emit(room=room_id, module='replay', event='frame', data=pic_b64)
                    except Exception as _emit_err:
                        print(f"⚠️ emit frame 失败: {_emit_err}")
                return screenshot

        # 如果设备没有serial属性，说明可能是Mock设备，已经在上面处理了
        if not hasattr(device, 'serial'):
            print("⚠️ 设备没有serial属性且没有screenshot方法，无法获取截图")
            return None

        # 使用subprocess直接获取字节数据，避免字符编码问题
        result = subprocess.run(
            f"adb -s {device.serial} exec-out screencap -p",
            shell=True,
            capture_output=True,
            timeout=10
        )

        if result.returncode == 0 and result.stdout:
            from PIL import Image
            # result.stdout 已经是字节数据，统一转成 base64 字符串
            pic_b64 = base64.b64encode(result.stdout).decode('utf-8')
            try:
                SocketIOHttpApiClient().emit(room=room_id, module='replay', event='frame', data=pic_b64)
            except Exception as _emit_err2:
                print(f"⚠️ emit frame 失败: {_emit_err2}")
            return Image.open(io.BytesIO(result.stdout))
        else:
            print("⚠️ 警告：screencap命令返回空数据或失败")
            return None
    except subprocess.TimeoutExpired:
        print("❌ 截图超时")
        return None
    except Exception as e:
        print(f"❌ ADB截图失败: {e}")

        # 尝试备用方法：转换为airtest设备
        try:
            from airtest.core.api import connect_device
            print("尝试使用airtest设备进行截图...")
            airtest_device = connect_device(f"Android:///{device.serial}")
            img = airtest_device.snapshot()
            # 将 PIL.Image 转为 base64
            pic_b64 = None
            from PIL import Image as _PILImage
            if isinstance(img, _PILImage.Image):
                buf = io.BytesIO()
                img.save(buf, format='PNG')
                pic_b64 = base64.b64encode(buf.getvalue()).decode('utf-8')
            if pic_b64:
                try:
                    SocketIOHttpApiClient().emit(room=room_id, module='replay', event='frame', data=pic_b64)
                except Exception as _emit_err3:
                    print(f"⚠️ emit frame 失败: {_emit_err3}")
            return img
        except Exception as e2:
            print(f"❌ Airtest截图也失败: {e2}")
            return None


def get_screenshot_safe(device, room_id=None, use_cache=False, cache_max_age=2):
    """
    安全获取设备截图，支持多种设备类型和截图方式，支持缓存
    
    Args:
        device: 设备对象（可以是airtest设备、mock设备等）
        room_id: WebSocket房间ID（可选，用于实时推送截图）
        use_cache: 是否使用缓存（默认False）
        cache_max_age: 缓存最大有效期（秒，默认2秒）
    
    Returns:
        PIL.Image对象，失败返回None
    """
    import io
    import base64
    import subprocess
    from socket_io_http_api_client import SocketIOHttpApiClient

    # 尝试从缓存获取截图
    if use_cache and hasattr(device, 'serial'):
        try:
            from device_connection_pool import get_device_connection_pool
            pool = get_device_connection_pool()
            cached_screenshot = pool.get_cached_screenshot(device.serial, cache_max_age)
            
            if cached_screenshot:
                print(f"📸 使用缓存截图（设备: {device.serial}）")
                
                # 如果需要推送到前端
                if room_id:
                    from PIL import Image as _PILImage
                    if isinstance(cached_screenshot, _PILImage.Image):
                        buf = io.BytesIO()
                        cached_screenshot.save(buf, format='PNG')
                        pic_b64 = base64.b64encode(buf.getvalue()).decode('utf-8')
                        try:
                            SocketIOHttpApiClient().emit(room=room_id, module='replay', event='frame', data=pic_b64)
                        except Exception as _emit_err:
                            print(f"⚠️ emit frame 失败: {_emit_err}")
                
                return cached_screenshot
        except Exception as cache_err:
            print(f"⚠️ 获取缓存截图失败: {cache_err}")

    try:
        screenshot = None
        
        # 优先使用设备自带的screenshot方法
        if hasattr(device, 'screenshot'):
            try:
                screenshot = device.screenshot()
                if screenshot:
                    # 如果需要推送到前端
                    if room_id:
                        pic_b64 = None
                        from PIL import Image as _PILImage
                        if isinstance(screenshot, _PILImage.Image):
                            buf = io.BytesIO()
                            screenshot.save(buf, format='PNG')
                            pic_b64 = base64.b64encode(buf.getvalue()).decode('utf-8')
                        if pic_b64:
                            try:
                                SocketIOHttpApiClient().emit(room=room_id, module='replay', event='frame', data=pic_b64)
                            except Exception as _emit_err:
                                print(f"⚠️ emit frame 失败: {_emit_err}")
                    
                    # 缓存截图
                    if use_cache and hasattr(device, 'serial'):
                        try:
                            from device_connection_pool import get_device_connection_pool
                            pool = get_device_connection_pool()
                            pool.cache_screenshot(device.serial, screenshot)
                        except Exception:
                            pass
                    
                    return screenshot
            except Exception as e:
                print(f"⚠️ 使用设备screenshot方法失败: {e}")

        # 如果设备有snapshot方法（airtest设备）
        if hasattr(device, 'snapshot'):
            try:
                screenshot = device.snapshot()
                if screenshot:
                    # 如果需要推送到前端
                    if room_id:
                        pic_b64 = None
                        from PIL import Image as _PILImage
                        if isinstance(screenshot, _PILImage.Image):
                            buf = io.BytesIO()
                            screenshot.save(buf, format='PNG')
                            pic_b64 = base64.b64encode(buf.getvalue()).decode('utf-8')
                        if pic_b64:
                            try:
                                SocketIOHttpApiClient().emit(room=room_id, module='replay', event='frame', data=pic_b64)
                            except Exception as _emit_err:
                                print(f"⚠️ emit frame 失败: {_emit_err}")
                    
                    # 缓存截图
                    if use_cache and hasattr(device, 'serial'):
                        try:
                            from device_connection_pool import get_device_connection_pool
                            pool = get_device_connection_pool()
                            pool.cache_screenshot(device.serial, screenshot)
                        except Exception:
                            pass
                    
                    return screenshot
            except Exception as e:
                print(f"⚠️ 使用设备snapshot方法失败: {e}")

        # 如果设备没有serial属性，说明可能是Mock设备，已经在上面处理了
        if not hasattr(device, 'serial'):
            print("⚠️ 设备没有serial属性且没有screenshot方法，无法获取截图")
            return None

        # 使用subprocess直接获取字节数据，避免字符编码问题
        result = subprocess.run(
            f"adb -s {device.serial} exec-out screencap -p",
            shell=True,
            capture_output=True,
            timeout=10
        )

        if result.returncode == 0 and result.stdout:
            from PIL import Image
            # result.stdout 已经是字节数据，统一转成 base64 字符串
            pic_b64 = base64.b64encode(result.stdout).decode('utf-8')
            try:
                SocketIOHttpApiClient().emit(room=room_id, module='replay', event='frame', data=pic_b64)
            except Exception as _emit_err2:
                print(f"⚠️ emit frame 失败: {_emit_err2}")
            
            screenshot = Image.open(io.BytesIO(result.stdout))
            
            # 缓存截图
            if use_cache:
                try:
                    from device_connection_pool import get_device_connection_pool
                    pool = get_device_connection_pool()
                    pool.cache_screenshot(device.serial, screenshot)
                except Exception:
                    pass
            
            return screenshot
        else:
            print("⚠️ 警告：screencap命令返回空数据或失败")
            return None
    except subprocess.TimeoutExpired:
        print("❌ 截图超时")
        return None
    except Exception as e:
        print(f"❌ ADB截图失败: {e}")
        return None


class ActionContext:
    """Action执行上下文类 - 统一接口"""

    def __init__(self, device, input_handler=None, config=None, screenshot_dir=None, script_name=None,
                 device_name=None, log_dir=None, queues=None, step_idx=None):
        """
        初始化ActionContext - 支持多种初始化方式

        Args:
            device: 设备对象
            input_handler: 输入处理器（可选，懒加载）
            config: 配置信息
            screenshot_dir: 截图目录
            script_name: 脚本名称
            device_name: 设备名称（兼容旧接口）
            log_dir: 日志目录（兼容旧接口）
            queues: 队列字典（兼容旧接口）
            step_idx: 步骤索引（用于多设备模式下的简化日志）
        """
        self.device = device
        self.input_handler = input_handler
        self.config = config or {}
        self.screenshot_dir = screenshot_dir or log_dir
        self.script_name = script_name
        self.step_idx = step_idx

        # 兼容旧接口
        self.device_name = device_name
        self.log_dir = log_dir or screenshot_dir
        self.queues = queues or {}

    @property
    def screenshot_queue(self):
        return self.queues.get('screenshot_queue')

    @property
    def click_queue(self):
        return self.queues.get('click_queue')

    @property
    def action_queue(self):
        return self.queues.get('action_queue')


class ActionResult:
    """Action执行结果类 - 统一接口"""

    def __init__(self, success=True, message="", screenshot_path=None, details=None, should_stop=False,
                 executed=None, should_continue=None):
        """
        初始化ActionResult - 支持多种初始化方式

        Args:
            success: 操作是否成功
            message: 结果消息
            screenshot_path: 截图路径
            details: 附加详细信息
            should_stop: 是否应该停止执行
            executed: 是否实际执行了操作（兼容旧接口）
            should_continue: 是否应该继续执行（兼容旧接口）
        """
        self.success = success
        self.message = message
        self.screenshot_path = screenshot_path
        self.details = details or {}
        self.should_stop = should_stop

        # 兼容旧接口
        self.executed = executed if executed is not None else success
        self.should_continue = should_continue if should_continue is not None else (not should_stop)

    def to_tuple(self):
        """转换为元组格式，兼容现有代码"""
        return (self.success, self.executed, self.should_continue)

    @classmethod
    def from_tuple(cls, tuple_result):
        """从元组创建ActionResult对象"""
        if len(tuple_result) == 3:
            success, executed, should_continue = tuple_result
            return cls(
                success=success,
                executed=executed,
                should_continue=should_continue,
                message="操作完成" if success else "操作失败"
            )
        else:
            # 如果元组格式不对，返回默认的失败结果
            return cls(success=False, message="无效的元组格式")


class ActionProcessor:
    """Action处理器类 - 支持新旧接口"""

    def __init__(self, device, device_name=None, log_txt_path=None, detect_buttons_func=None, context=None):
        """
        初始化Action处理器 - 支持多种初始化方式

        新接口参数:
            device: 设备对象
            input_handler: 输入处理器（可选，懒加载）
            ai_service: AI服务（可选）
            config: 配置字典

        旧接口参数（兼容性）:
            device_name: 设备名称
            log_txt_path: 日志文件路径
            detect_buttons_func: AI检测按钮的函数
        """
        self.device = device
        self.input_handler = None
        self.ai_service = None
        self.config = {}        # 兼容旧接口
        self.device_name = device_name
        self.log_txt_path = log_txt_path
        self.detect_buttons = detect_buttons_func
        self.device_account = None
        # 记录最近一次截图的绝对路径，供步骤结果回填
        self._last_screenshot_path = None
        
        # 截图缓存相关
        self._use_screenshot_cache = True  # 是否启用截图缓存
        self._device_pool = None  # 设备连接池引用（懒加载）
        
        # 性能日志记录器（懒加载）
        self._perf_logger = None
        self._step_metrics = {}  # 当前步骤的性能指标

    def set_device_account(self, device_account):
        """设置设备账号信息"""
        self.device_account = device_account
    
    def _get_perf_logger(self):
        """获取性能日志记录器（懒加载）"""
        if self._perf_logger is None:
            try:
                from performance_logger import get_performance_logger
                self._perf_logger = get_performance_logger()
            except ImportError:
                # 如果性能日志模块不可用，使用空对象模式
                class DummyLogger:
                    def measure_time(self, *args, **kwargs):
                        from contextlib import contextmanager
                        @contextmanager
                        def dummy():
                            yield
                        return dummy()
                    def log_step_performance(self, *args, **kwargs): pass
                    def log_success(self, *args, **kwargs): pass
                    def log_failure(self, *args, **kwargs): pass
                    def log_retry(self, *args, **kwargs): pass
                    def log_resource_usage(self, *args, **kwargs): pass
                self._perf_logger = DummyLogger()
        return self._perf_logger
    
    def _record_step_metric(self, metric_name: str, duration: float):
        """记录步骤性能指标"""
        self._step_metrics[metric_name] = duration
    
    def _log_step_performance(self, step_name: str):
        """记录步骤性能日志"""
        if self._step_metrics:
            logger = self._get_perf_logger()
            logger.log_step_performance(step_name, self._step_metrics)
            self._step_metrics = {}  # 清空指标

    def _normalize_text(self, txt):
        """规范化文本：去除空格并转小写，便于严格比较"""
        if txt is None:
            return ""
        s = str(txt)
        s = s.strip().lower().replace(" ", "")
        return s

    def _prepare_keywords(self, ocr_keywords):
        """
        预处理OCR关键字，返回关键字列表和规范化集合
        
        Args:
            ocr_keywords: 原始关键字字符串（逗号分隔）
            
        Returns:
            tuple: (keywords_list, normalized_keywords)
                - keywords_list: 关键字列表
                - normalized_keywords: 规范化后的关键字集合
        """
        # 处理关键词列表
        keywords_list = [
            k.strip() for k in str(ocr_keywords or "").split(",") if k and k.strip()
        ]
        
        # 规范化关键词集合，用于严格匹配
        normalized_keywords = set(
            self._normalize_text(k) for k in keywords_list if k
        )
        
        return keywords_list, normalized_keywords

    def _auto_allocate_device_account(self):
        """自动为设备分配账号（智能重试机制）"""
        try:
            # 尝试获取设备序列号
            device_serial = getattr(self.device, 'serial', None)
            if not device_serial:
                device_serial = self.device_name

            if not device_serial:
                print("⚠️ 无法获取设备序列号，无法自动分配账号")
                return False

            print(f"🔄 正在为设备 {device_serial} 自动分配账号...")

            # 导入账号管理器
            try:
                from account_manager import get_account_manager
                account_manager = get_account_manager()
            except ImportError as e:
                print(f"❌ 无法导入账号管理器: {e}")
                return False

            # 尝试分配账号
            device_account = account_manager.allocate_account(device_serial)

            if device_account:
                self.set_device_account(device_account)
                username, password = device_account
                print(f"✅ 自动为设备 {device_serial} 分配账号成功: {username}")
                return True
            else:
                print(f"❌ 无法为设备 {device_serial} 分配账号（账号池可能已满）")

                # 获取详细的分配状态信息
                try:
                    total_accounts = len(account_manager.accounts)
                    available_count = account_manager.get_available_accounts_count()
                    allocation_status = account_manager.get_allocation_status()

                    print(f"📊 账号池状态: 总账号数={total_accounts}, 可用={available_count}, 已分配={len(allocation_status)}")

                    if allocation_status:
                        print("📋 当前分配状态:")
                        for dev_serial, username in list(allocation_status.items())[:5]:  # 只显示前5个
                            print(f"   - {dev_serial}: {username}")
                        if len(allocation_status) > 5:
                            print(f"   ... 还有 {len(allocation_status) - 5} 个分配")

                except Exception as status_e:
                    print(f"⚠️ 获取账号状态信息失败: {status_e}")

                return False

        except Exception as e:
            print(f"❌ 自动账号分配过程中发生异常: {e}")
            import traceback
            traceback.print_exc()
            return False

    def process_action(self, step, step_idx, log_dir):
        """
        处理单个action步骤

        Args:
            step: 步骤配置
            step_idx: 步骤索引
            log_dir: 日志目录

        Returns:
            tuple: (success, executed, should_continue)
        """
        result = self._process_action(step, step_idx, log_dir)
        # 支持ActionResult和旧式tuple返回，确保统一输出tuple
        if isinstance(result, ActionResult):
            return result.to_tuple()
        else:
            return result

    def _process_action(self, step, step_idx, log_dir):
        """处理action步骤"""
        # 确保每个步骤开始时清空上一次的截图记录，避免把上一步的截图误填到当前步骤

        step_action = step.get("action", "click")
        step_yolo_class = step.get("yolo_class", "")  # 修复: 确保step_yolo_class已定义
        step_remark = step.get("remark", "")
        
        # 打印清晰的步骤开始标记
        device_serial = getattr(self.device, 'serial', 'Unknown')
        print("\n" + "="*80)
        print(f"📱 [{device_serial}] 步骤 #{step_idx + 1} | 操作: {step_action}")
        if step_remark:
            print(f"📝 {step_remark}")
        print("="*80)
        
        # 获取重试配置(默认值: max_retries=3, retry_interval=1秒)
        max_retries = step.get("max_retries", 3)
        base_retry_interval = step.get("retry_interval", 1)
        use_exponential_backoff = step.get("exponential_backoff", True)  # 默认启用指数退避
        
        # 预先初始化result变量，避免未赋值错误
        result = ActionResult(
            success=False,
            message="步骤未执行",
            details={"operation": step_action, "status": "not_executed"}
        )
        
        # 执行步骤(带智能重试机制)
        for attempt in range(max_retries):
            if attempt > 0:
                # 计算重试间隔：指数退避或固定间隔
                if use_exponential_backoff:
                    # 指数退避：1s, 2s, 4s...（最大8秒）
                    retry_interval = min(base_retry_interval * (2 ** (attempt - 1)), 8)
                else:
                    retry_interval = base_retry_interval
                
                print(f"🔄 [步骤 {step_idx + 1}] 第 {attempt + 1}/{max_retries} 次重试（间隔{retry_interval}秒）...")
                time.sleep(retry_interval)
            
            result = self._execute_single_action(step, step_idx, log_dir, step_action, step_yolo_class)
            
            # 如果成功或者是不需要重试的操作,直接返回
            if result.success or step_action in ["delay", "log", "device_preparation"]:
                if attempt > 0:
                    print(f"✅ [步骤 {step_idx + 1}] 重试成功 (第 {attempt + 1} 次尝试)")
                break
            else:
                # 记录失败原因，帮助调试
                failure_reason = result.details.get("failure_reason", "未知原因")
                print(f"   失败原因: {failure_reason}")
        else:
            # 所有重试都失败
            print(f"❌ [步骤 {step_idx + 1}] 重试 {max_retries} 次后仍然失败")
            result.details["retry_attempts"] = max_retries
            result.details["all_retries_failed"] = True
        
        # 统一返回 ActionResult，便于上层获取 screenshot_path
        if isinstance(result, tuple):
            result = ActionResult.from_tuple(result)
        elif not isinstance(result, ActionResult):
            result = ActionResult(success=False, message=str(result))

        # 如果没有显式截图路径但最近一次截图存在，补全
        if not getattr(result, 'screenshot_path', None) and getattr(self, '_last_screenshot_path', None):
            result.screenshot_path = self._last_screenshot_path
        return result
    
    def _execute_single_action(self, step, step_idx, log_dir, step_action, step_yolo_class):
        """执行单次操作(不包含重试逻辑)"""
        result = ActionResult(
            success=False,
            message="步骤未执行",
            details={"operation": step_action, "status": "not_executed"}
        )

        if step_action == "delay":
            result = self._handle_delay(step, step_idx, log_dir)

        elif step_action == "device_preparation":
            result = self._handle_device_preparation(step, step_idx)

        elif step_action == "app_start":
            result = self._handle_app_start(step, step_idx)

        elif step_action == "app_stop":
            result = self._handle_app_stop(step, step_idx)

        elif step_action == "log":
            result = self._handle_log(step, step_idx)

        # 处理新的3个关键功能

        elif step_action == "wait_for_appearance":
            # 直接执行等待出现步骤，内部已支持 execute_action=click 的自动点击
            result = self._handle_wait_for_appearance(step, step_idx, log_dir)
        elif step_action == "wait_for_stable":
            result = self._handle_wait_for_stable(step, step_idx, log_dir)

        elif step_action == "retry_until_success":
            result = self._handle_retry_until_success(step, step_idx, log_dir)

        # 处理现有功能

        elif step_action == "wait_if_exists":
            result = self._handle_wait_if_exists(step, step_idx, log_dir)

        elif step_action == "swipe":
            result = self._handle_swipe(step, step_idx)

        elif step_action == "input":
            # UI输入方式已废弃，请使用 retry_until_success 配合 AI 定位
            print("❌ 'input' 操作已废弃")
            print("💡 请使用 'retry_until_success' 操作，配合 'yolo_class' 和 'execute_action=input'")
            print("   示例: {\"action\": \"retry_until_success\", \"execute_action\": \"input\", \"yolo_class\": \"输入框类别\", \"text\": \"输入内容\"}")
            result = ActionResult(
                success=False,
                message="input操作已废弃，请使用retry_until_success配合AI定位",
                details={"operation": "input", "error": "deprecated", "suggestion": "use retry_until_success with AI"}
            )

        elif step_action == "checkbox":
            # UI checkbox方式已废弃，请使用 AI 点击方式
            print("❌ 'checkbox' 操作已废弃")
            print("💡 请使用 'ai_detection_click' 或 'retry_until_success' 操作来点击checkbox")
            print("   示例: {\"action\": \"ai_detection_click\", \"yolo_class\": \"checkbox类别\"}")
            result = ActionResult(
                success=False,
                message="checkbox操作已废弃，请使用AI检测点击",
                details={"operation": "checkbox", "error": "deprecated", "suggestion": "use ai_detection_click"}
            )

        elif step_action == "wait_for_disappearance":
            result = self._handle_wait_for_disappearance(step, step_idx, log_dir)

        # 关键修复：优先处理ai_detection_click动作
        elif step_action == "ai_detection_click":
            print(f"🎯 执行AI检测点击操作")
            result = self._handle_ai_detection_click(step, step_idx, log_dir)
        elif step_action == "fallback_click":
            print(f"🎯 执行备选点击操作")
            result = self._handle_fallback_click(step, step_idx, log_dir)
        elif step_action == "click":
            # 检查是否有execute_action字段（点击后执行其他操作click/input/checkbox）
            execute_action = step.get("execute_action")
            if execute_action:
                print(f"🎯 检测到组合操作: click + {execute_action}")
                result = self._handle_click_with_execute_action(step, step_idx, log_dir)
            else:
                # 默认处理：尝试AI检测点击

                if step_action == "fallback_click" and "relative_x" in step and "relative_y" in step:
                    result = self._handle_fallback_click(step, step_idx, log_dir)

                elif step_yolo_class and step_yolo_class != "fallback_click":
                    # 对于Priority模式脚本，如果有yolo_class字段，执行AI检测点击
                    print(f"🎯 检测到yolo_class字段: {step_yolo_class}，执行AI检测点击")
                    result = self._handle_ai_detection_click(step, step_idx, log_dir)
                elif step.get("ocr_keywords"):
                    # 修复：添加OCR检测分支，使用现有的AI检测方法（它已支持OCR）
                    ocr_keywords = step.get("ocr_keywords")
                    print(f"🎯 检测到OCR关键字: {ocr_keywords}，执行OCR检测点击")
                    result = self._handle_ai_detection_click(step, step_idx, log_dir)
                else:
                    result = ActionResult(
                        success=False,
                        message="步骤类型不匹配或无法识别",
                        details={"operation": step_action, "step_yolo_class": step_yolo_class}
                    )

        # 统一返回 ActionResult，便于上层获取 screenshot_path
        if isinstance(result, tuple):
            result = ActionResult.from_tuple(result)
        elif not isinstance(result, ActionResult):
            result = ActionResult(success=False, message=str(result))

        # 如果没有显式截图路径但最近一次截图存在，补全
        if not getattr(result, 'screenshot_path', None) and getattr(self, '_last_screenshot_path', None):
            result.screenshot_path = self._last_screenshot_path
        return result

    def _handle_delay(self, step, step_idx, log_dir=None):
        """处理延时步骤"""
        delay_seconds = step.get("seconds", 1)
        step_remark = step.get("remark", "")

        print(f"延时 {delay_seconds} 秒: {step_remark}")
        time.sleep(delay_seconds)

        # 创建screen对象以支持报告截图显示
        screen_data = self._create_unified_screen_object(
            log_dir,
            pos_list=[],
            confidence=1.0,
            rect_info=[]
        )

        # 记录延时日志
        timestamp = time.time()
        delay_entry = {
            "tag": "function",
            "depth": 1,
            "time": timestamp,
            "data": {
                "name": "delay",
                "call_args": {"seconds": delay_seconds},
                "start_time": timestamp - delay_seconds,
                "ret": None,
                "end_time": timestamp,
                "desc": step_remark or f"延时 {delay_seconds} 秒",
                "title": f"#{step_idx+1} {step_remark or f'延时 {delay_seconds} 秒'}"
            }
        }

        # 添加screen对象到日志条目（如果可用）
        if screen_data:
            delay_entry["data"]["screen"] = screen_data        # 添加 executed 字段到日志条目
        delay_entry["data"]["executed"] = True
        self._write_log_entry(delay_entry)

        return ActionResult(
            success=True,
            message=f"延时 {delay_seconds} 秒完成",
            details={
                "operation": "delay",
                "duration_seconds": delay_seconds,
                "has_screenshot": screen_data is not None
            }
        )

    def _handle_fallback_click(self, step, step_idx, log_dir):
        """处理备选点击步骤（使用相对坐标）"""
        step_remark = step.get("remark", "")

        if "relative_x" not in step or "relative_y" not in step:
            print(f"错误: fallback click 步骤缺少相对坐标信息")
            return ActionResult(
                success=False,
                message="fallback_click 步骤缺少相对坐标信息",
                details={"operation": "fallback_click", "error": "missing_relative_coordinates"}
            )

        try:
            # 获取屏幕截图以获取分辨率
            screenshot = get_device_screenshot(self.device)
            if screenshot is None:
                print(f"❌ 无法获取屏幕截图")
                return ActionResult(
                    success=False,
                    message="无法获取屏幕截图",
                    details={"operation": "fallback_click", "error": "screenshot_failed"}
                )

            import cv2
            import numpy as np
            frame = cv2.cvtColor(np.array(screenshot), cv2.COLOR_RGB2BGR)
            height, width = frame.shape[:2]

            # 计算绝对坐标
            rel_x = float(step["relative_x"])
            rel_y = float(step["relative_y"])
            abs_x = int(width * rel_x)
            abs_y = int(height * rel_y)

            print(f"执行备选点击: 相对位置 ({rel_x}, {rel_y}) -> 绝对位置 ({abs_x}, {abs_y})")

            # 执行点击操作
            self.device.shell(f"input tap {abs_x} {abs_y}")

            # 创建screen对象以支持报告截图显示
            screen_data = self._create_unified_screen_object(
                log_dir,
                pos_list=[[abs_x, abs_y]],
                confidence=1.0,
                rect_info=[{
                    "left": max(0, abs_x - 50),
                    "top": max(0, abs_y - 50),
                    "width": 100,
                    "height": 100
                }]
            )

            # 记录点击日志
            timestamp = time.time()
            click_entry = {
                "tag": "function",
                "depth": 1,
                "time": timestamp,
                "data": {
                    "name": "touch",
                    "call_args": {"v": [abs_x, abs_y]},
                    "start_time": timestamp,
                    "ret": [abs_x, abs_y],
                    "end_time": timestamp + 0.1,
                    "desc": step_remark or f"备选点击({rel_x:.3f}, {rel_y:.3f})",
                    "title": f"#{step_idx+1} {step_remark or f'备选点击({rel_x:.3f}, {rel_y:.3f})'}"
                }
            }            # 添加screen对象到日志条目
            if screen_data:
                click_entry["data"]["screen"] = screen_data            # 添加 executed 字段到日志条目
            click_entry["data"]["executed"] = True

            self._write_log_entry(click_entry)

            return ActionResult(
                success=True,
                message=f"备选点击成功: ({rel_x:.3f}, {rel_y:.3f}) -> ({abs_x}, {abs_y})",
                details={
                    "operation": "fallback_click",
                    "relative_position": {"x": rel_x, "y": rel_y},
                    "absolute_position": {"x": abs_x, "y": abs_y},
                    "screen_size": {"width": width, "height": height}
                }
            )

        except Exception as e:
            print(f"❌ 备选点击过程中发生异常: {e}")
            import traceback
            traceback.print_exc()
            return ActionResult(
                success=False,
                message=f"备选点击失败: {str(e)}",
                details={"operation": "fallback_click", "error": str(e)}
            )

    def _determine_detection_mode(self, step):
        """判断检测模式: yolo_only | ocr_only | yolo_then_ocr"""
        # 1. 显式指定模式
        if "detection_mode" in step:
            mode = step["detection_mode"]
            if mode in ["yolo_only", "ocr_only", "yolo_then_ocr"]:
                return mode
            else:
                print(f"⚠️ 无效的detection_mode: {mode}, 将自动推断")
        
        # 2. 自动推断模式
        has_yolo = bool(step.get("yolo_class"))
        has_ocr = bool(step.get("ocr_keywords"))
        
        if has_yolo and has_ocr:
            return "yolo_then_ocr"  # YOLO预检+OCR过滤
        elif has_yolo:
            return "yolo_only"      # 纯YOLO
        elif has_ocr:
            return "ocr_only"       # 纯OCR
        else:
            return None  # 无效配置

    def _handle_ai_detection_click(self, step, step_idx, log_dir):
        print(f"[DEBUG] 进入_handle_ai_detection_click, step={step}, step_idx={step_idx}, log_dir={log_dir}")

        step_remark = step.get("remark", "")
        
        # 判断检测模式
        detection_mode = self._determine_detection_mode(step)
        
        if detection_mode is None:
            print(f"❌ 错误: AI检测点击步骤必须指定 yolo_class 或 ocr_keywords")
            timestamp = time.time()
            ai_entry = {
                "tag": "function",
                "depth": 1,
                "time": timestamp,
                "data": {
                    "name": "ai_detection_click",
                    "call_args": {},
                    "start_time": timestamp,
                    "ret": None,
                    "end_time": timestamp,
                    "desc": step_remark or "AI检测点击",
                    "executed": False
                }
            }
            self._write_log_entry(ai_entry)
            return ActionResult(
                success=False,
                message="AI检测点击步骤必须指定 yolo_class 或 ocr_keywords",
                details={"operation": "ai_detection_click", "error": "invalid_config"},
                executed=False
            )
        
        print(f"🔍 检测模式: {detection_mode}")
        
        # 根据模式分发到不同的处理方法
        if detection_mode == "yolo_only":
            return self._handle_yolo_only_detection(step, step_idx, log_dir)
        elif detection_mode == "ocr_only":
            return self._handle_ocr_only_detection(step, step_idx, log_dir)
        elif detection_mode == "yolo_then_ocr":
            return self._handle_yolo_then_ocr_detection(step, step_idx, log_dir)
    
    def _create_failed_result(self, target, remark, error_type):
        """创建失败结果的辅助方法"""
        timestamp = time.time()
        ai_entry = {
            "tag": "function",
            "depth": 1,
            "time": timestamp,
            "data": {
                "name": "ai_detection_click",
                "call_args": {"target": target},
                "start_time": timestamp,
                "ret": None,
                "end_time": timestamp,
                "desc": remark or "AI检测点击",
                "executed": False
            }
        }
        self._write_log_entry(ai_entry)
        return ActionResult(
            success=False,
            message=f"AI检测失败: {error_type}",
            details={"operation": "ai_detection_click", "error": error_type},
            executed=False
        )
    
    def _handle_yolo_only_detection(self, step, step_idx, log_dir):
        """处理纯YOLO检测模式"""
        step_class = step.get("yolo_class")
        step_remark = step.get("remark", "")
        print(f"[YOLO模式] 目标类别: {step_class}")

        try:
            screenshot = get_device_screenshot(self.device)
            if screenshot is None:
                print(f"❌ 无法获取设备屏幕截图")
                return self._create_failed_result(step_class, step_remark, "screenshot_failed")
            
            frame = cv2.cvtColor(np.array(screenshot), cv2.COLOR_RGB2BGR)

            if self.detect_buttons:
                step_confidence = step.get("confidence", 0.35)
                print(f"🎯 YOLO置信度阈值: {step_confidence}")
                
                # 纯YOLO模式,不使用OCR
                success, detection_result = self.detect_buttons(
                    frame, 
                    target_class=step_class, 
                    conf_threshold=step_confidence,
                    use_ocr=False
                )
                print(f"🔍 YOLO检测输出: success={success}")

                timestamp = time.time()
                if success and detection_result[0] is not None:
                    x, y, detected_class, _ = detection_result

                    screen_data = self._create_unified_screen_object(
                        log_dir,
                        pos_list=[[int(x), int(y)]],
                        confidence=step_confidence,
                        rect_info=[{"left":int(x)-20,"top":int(y)-20,"width":40,"height":40}]
                    )

                    print(f"🖱️ 执行点击操作: input tap {int(x)} {int(y)}")
                    timestamp_before_click = time.time()
                    self.device.shell(f"input tap {int(x)} {int(y)}")
                    timestamp_after_click = time.time()
                    print(f"✅ 点击命令已发送")

                    call_args = {
                        "detection_mode": "yolo_only",
                        "target_class": step_class, 
                        "position": [int(x), int(y)]
                    }
                    
                    ai_entry = {
                        "tag": "function",
                        "depth": 1,
                        "time": timestamp,
                        "data": {
                            "name": "ai_detection_click",
                            "call_args": call_args,
                            "start_time": timestamp,
                            "ret": [int(x), int(y)],
                            "end_time": timestamp,
                            "desc": step_remark or f"AI检测点击({step_class})",
                            "executed": True
                        }
                    }
                    if screen_data:
                        ai_entry["data"]["screen"] = screen_data
                    self._write_log_entry(ai_entry)
                    return ActionResult(
                        success=True,
                        message=f"AI检测点击成功: {step_class}",
                        details={"operation": "ai_detection_click", "target_class": step_class, "position": [int(x), int(y)]},
                        executed=True
                    )
                else:
                    # 检测失败，不记录日志，只返回失败结果
                    return ActionResult(
                        success=False,
                        message=f"AI检测未命中: {step_class}",
                        details={"operation": "ai_detection_click", "target_class": step_class},
                        executed=False
                    )
            else:
                print(f"❌ AI检测功能不可用")
                timestamp = time.time()
                ai_entry = {
                    "tag": "function",
                    "depth": 1,
                    "time": timestamp,
                    "data": {
                        "name": "ai_detection_click",
                        "call_args": {"target_class": step_class},
                        "start_time": timestamp,
                        "ret": None,
                        "end_time": timestamp,
                        "desc": step_remark or f"AI检测点击({step_class})",
                        "executed": False
                    }
                }
                self._write_log_entry(ai_entry)
                return ActionResult(
                    success=False,
                    message="AI检测功能不可用",
                    details={"operation": "ai_detection_click", "error": "ai_detection_unavailable"},
                    executed=False
                )

        except Exception as e:
            print(f"❌ AI检测点击过程中发生异常: {e}")
            import traceback
            traceback.print_exc()
            timestamp = time.time()
            ai_entry = {
                "tag": "function",
                "depth": 1,
                "time": timestamp,
                "data": {
                    "name": "ai_detection_click",
                    "call_args": {"target_class": step_class},
                    "start_time": timestamp,
                    "ret": None,
                    "end_time": timestamp,
                    "desc": step_remark or f"AI检测点击({step_class})",
                    "executed": False
                }
            }
            self._write_log_entry(ai_entry)
            return ActionResult(
                success=False,
                message=f"AI检测点击异常: {str(e)}",
                details={"operation": "ai_detection_click", "error": str(e)},
                executed=False
            )
    
    def _analyze_ocr_failure(self, result, ocr_keywords, ocr_min_score):
        """
        分析OCR检测失败的原因
        
        Args:
            result: OCR检测结果字典（可能为None）
            ocr_keywords: 目标关键字
            ocr_min_score: 置信度阈值
            
        Returns:
            str: 失败原因描述
        """
        if result is None:
            return "OCR检测返回空结果"
        
        # 检查是否有识别到任何文本
        all_texts = result.get("all_texts", [])
        all_scores = result.get("all_scores", [])
        
        if not all_texts:
            return "OCR未识别到任何文本（可能是界面正在加载或文本太小）"
        
        # 检查置信度过滤
        high_conf_texts = [t for t, s in zip(all_texts, all_scores) if s >= ocr_min_score]
        if not high_conf_texts:
            max_score = max(all_scores) if all_scores else 0
            return f"所有识别文本的置信度都低于阈值{ocr_min_score}（最高置信度: {max_score:.2f}）"
        
        # 检查关键字匹配
        if ocr_keywords:
            keywords_list = [k.strip() for k in ocr_keywords.split(',')]
            return f"识别到{len(high_conf_texts)}个高置信度文本，但都不包含关键字: {keywords_list}"
        
        return "OCR检测失败（未知原因）"
    
    def _handle_ocr_only_detection(self, step, step_idx, log_dir):
        """
        处理纯OCR检测模式(全屏搜索)
        
        优化说明:
        - 全屏OCR检测使用更低的默认置信度阈值(0.4)
        - 因为全屏检测范围大，文本可能较小或背景复杂
        - 可通过step配置覆盖默认值
        """
        ocr_keywords = step.get("ocr_keywords")
        # 全屏OCR使用更低的默认阈值(0.4)，因为检测范围大、文本可能较小
        ocr_min_score = step.get("ocr_min_score", 0.4)
        step_remark = step.get("remark", "")
        
        # 支持灵活的OCR匹配策略
        ocr_match_method = step.get("ocr_match_method", "best")  # 默认最佳匹配
        ocr_match_method_desc = step.get("ocr_match_method_desc", "")
        
        print(f"[OCR模式] 关键字: {ocr_keywords}, 最小置信度: {ocr_min_score} (全屏检测)")
        print(f"🎯 OCR匹配策略: {ocr_match_method}")
        if ocr_match_method == "desc" and ocr_match_method_desc:
            print(f"🎯 位置描述: {ocr_match_method_desc}")
        
        try:
            screenshot = get_device_screenshot(self.device)
            if screenshot is None:
                print(f"❌ 无法获取设备屏幕截图")
                return self._create_failed_result(ocr_keywords, step_remark, "screenshot_failed")
            
            frame = cv2.cvtColor(np.array(screenshot), cv2.COLOR_RGB2BGR)
            
            # 根据匹配策略调用不同的OCR处理方法
            if ocr_match_method == "first":
                success, result = self._ocr_match_first_strategy(frame, ocr_keywords, ocr_min_score)
            elif ocr_match_method == "desc":
                success, result = self._ocr_match_desc_strategy(frame, ocr_keywords, ocr_match_method_desc, ocr_min_score)
            else:
                # 默认使用最佳匹配策略 (best)
                from apps.scripts.replay_script import perform_fullscreen_ocr_detection
                success, result = perform_fullscreen_ocr_detection(
                    frame,
                    ocr_keywords=ocr_keywords,
                    ocr_min_score=ocr_min_score
                )
            
            timestamp = time.time()
            if success and result:
                x, y = result["position"]
                ocr_text = result["text"]
                ocr_score = result["score"]
                
                print(f"✅ OCR找到文本: {ocr_text} (置信度: {ocr_score:.2f}) 位置: ({x}, {y})")
                
                # 验证坐标是否在屏幕范围内
                screen_width = frame.shape[1]
                screen_height = frame.shape[0]
                print(f"🔍 屏幕尺寸: {screen_width}x{screen_height}, 点击坐标: ({x}, {y})")
                
                if x < 0 or x > screen_width or y < 0 or y > screen_height:
                    print(f"⚠️ 警告: 点击坐标超出屏幕范围!")
                
                screen_data = self._create_unified_screen_object(
                    log_dir,
                    pos_list=[[int(x), int(y)]],
                    confidence=ocr_score,
                    rect_info=[{"left":int(x)-20,"top":int(y)-20,"width":40,"height":40}]
                )
                
                print(f"🖱️ 执行点击操作: input tap {int(x)} {int(y)}")
                self.device.shell(f"input tap {int(x)} {int(y)}")
                print(f"✅ 点击命令已发送")
                
                call_args = {
                    "detection_mode": "ocr_only",
                    "ocr_keywords": ocr_keywords,
                    "ocr_text": ocr_text,
                    "ocr_score": ocr_score,
                    "position": [int(x), int(y)]
                }
                
                ai_entry = {
                    "tag": "function",
                    "depth": 1,
                    "time": timestamp,
                    "data": {
                        "name": "ai_detection_click",
                        "call_args": call_args,
                        "start_time": timestamp,
                        "ret": [int(x), int(y)],
                        "end_time": timestamp,
                        "desc": step_remark or f"OCR检测点击({ocr_keywords})",
                        "executed": True
                    }
                }
                if screen_data:
                    ai_entry["data"]["screen"] = screen_data
                self._write_log_entry(ai_entry)
                
                return ActionResult(
                    success=True,
                    message=f"OCR检测点击成功: {ocr_text}",
                    details={"operation": "ai_detection_click", "ocr_text": ocr_text, "position": [int(x), int(y)]},
                    executed=True
                )
            else:
                # 分析失败原因
                failure_reason = self._analyze_ocr_failure(result, ocr_keywords, ocr_min_score)
                print(f"❌ OCR未找到匹配的文本: {ocr_keywords}")
                print(f"   {failure_reason}")
                
                return ActionResult(
                    success=False,
                    message=f"OCR未找到匹配的文本: {ocr_keywords}",
                    details={
                        "operation": "ai_detection_click", 
                        "ocr_keywords": ocr_keywords,
                        "failure_reason": failure_reason,
                        "ocr_min_score": ocr_min_score
                    },
                    executed=False
                )
                
        except Exception as e:
            print(f"❌ OCR检测过程中发生异常: {e}")
            import traceback
            traceback.print_exc()
            return ActionResult(
                success=False,
                message=f"OCR检测异常: {str(e)}",
                details={"operation": "ai_detection_click", "error": str(e)},
                executed=False
            )
    
    def _handle_yolo_then_ocr_detection(self, step, step_idx, log_dir):
        """处理YOLO+OCR组合检测模式"""
        yolo_class = step.get("yolo_class")
        ocr_keywords = step.get("ocr_keywords")
        step_remark = step.get("remark", "")
        print(f"[YOLO+OCR模式] YOLO类别: {yolo_class}, OCR关键字: {ocr_keywords}")
        
        try:
            screenshot = get_device_screenshot(self.device)
            if screenshot is None:
                print(f"❌ 无法获取设备屏幕截图")
                return self._create_failed_result(f"{yolo_class}+{ocr_keywords}", step_remark, "screenshot_failed")
            
            frame = cv2.cvtColor(np.array(screenshot), cv2.COLOR_RGB2BGR)
            
            if self.detect_buttons:
                step_confidence = step.get("confidence", 0.35)
                ocr_min_score = step.get("ocr_min_score", 0.5)
                
                print(f"🎯 YOLO置信度: {step_confidence}, OCR最小置信度: {ocr_min_score}")
                
                # YOLO+OCR组合模式
                success, detection_result = self.detect_buttons(
                    frame,
                    target_class=yolo_class,
                    conf_threshold=step_confidence,
                    use_ocr=True,
                    ocr_keywords=ocr_keywords,
                    ocr_min_score=ocr_min_score
                )
                
                timestamp = time.time()
                if success and detection_result[0] is not None:
                    x, y, detected_class, ocr_result = detection_result
                    
                    screen_data = self._create_unified_screen_object(
                        log_dir,
                        pos_list=[[int(x), int(y)]],
                        confidence=step_confidence,
                        rect_info=[{"left":int(x)-20,"top":int(y)-20,"width":40,"height":40}]
                    )
                    
                    print(f"🖱️ 执行点击操作: input tap {int(x)} {int(y)}")
                    timestamp_before_click = time.time()
                    self.device.shell(f"input tap {int(x)} {int(y)}")
                    timestamp_after_click = time.time()
                    print(f"✅ 点击命令已发送")
                    
                    call_args = {
                        "detection_mode": "yolo_then_ocr",
                        "target_class": yolo_class,
                        "position": [int(x), int(y)]
                    }
                    if ocr_result:
                        call_args["ocr_texts"] = ocr_result.get("texts", [])
                        call_args["ocr_scores"] = ocr_result.get("scores", [])
                        call_args["ocr_matched"] = ocr_result.get("has_match", False)
                    
                    ai_entry = {
                        "tag": "function",
                        "depth": 1,
                        "time": timestamp,
                        "data": {
                            "name": "ai_detection_click",
                            "call_args": call_args,
                            "start_time": timestamp,
                            "ret": [int(x), int(y)],
                            "end_time": timestamp,
                            "desc": step_remark or f"YOLO+OCR检测点击({yolo_class}+{ocr_keywords})",
                            "executed": True
                        }
                    }
                    if screen_data:
                        ai_entry["data"]["screen"] = screen_data
                    self._write_log_entry(ai_entry)
                    
                    return ActionResult(
                        success=True,
                        message=f"YOLO+OCR检测点击成功: {yolo_class}",
                        details={"operation": "ai_detection_click", "target_class": yolo_class, "position": [int(x), int(y)]},
                        executed=True
                    )
                else:
                    print(f"❌ YOLO+OCR未找到匹配目标")
                    return ActionResult(
                        success=False,
                        message=f"YOLO+OCR未找到匹配目标: {yolo_class}+{ocr_keywords}",
                        details={"operation": "ai_detection_click", "target_class": yolo_class, "ocr_keywords": ocr_keywords},
                        executed=False
                    )
            else:
                print(f"❌ AI检测功能不可用")
                return self._create_failed_result(step_class, step_remark, "ai_detection_unavailable")
                
        except Exception as e:
            print(f"❌ YOLO+OCR检测过程中发生异常: {e}")
            import traceback
            traceback.print_exc()
            return ActionResult(
                success=False,
                message=f"YOLO+OCR检测异常: {str(e)}",
                details={"operation": "ai_detection_click", "error": str(e)},
                executed=False
            )

    def _handle_device_preparation(self, step, step_idx):
        """处理设备预处理步骤"""
        # 检查是否已经执行过预处理（避免重复执行）
        if hasattr(self, '_device_preparation_executed') and self._device_preparation_executed:
            print("⏭️ 设备预处理已执行过，跳过重复执行")
            return ActionResult(
                success=True,
                message="设备预处理已执行（跳过）",
                details={"operation": "device_preparation", "skipped": True}
            )
        
        config = {
            "check_usb": step.get("check_usb",True),
            "setup_wireless": step.get("setup_wireless",False),
            "auto_handle_dialog": step.get("auto_handle_dialog",True),
            "handle_screen_lock": step.get("handle_screen_lock",True),
            "setup_input_method": step.get("setup_input_method",True),
            "save_logs": step.get("save_logs",False),
            "remark":step.get("remark")
        }

        print(f"🔧 开始设备预处理: {config['remark']}")
        print(
            "📋 预处理参数: USB检查={}, 无线设置={}, 弹窗处理={},屏幕锁定={}, 输入法设置={}, 保存日志={}".format(
                config["check_usb"],
                config["setup_wireless"],
                config["auto_handle_dialog"],
                config["handle_screen_lock"],
                config["setup_input_method"],
                config["save_logs"]
            )
        )

        # 初始化success变量，默认为True
        success = True

        try:
            device_manager = None
            from enhanced_device_preparation_manager import EnhancedDevicePreparationManager
            # 检查EnhancedDevicePreparationManager是否可用
            if EnhancedDevicePreparationManager is None:
                print("❌ EnhancedDevicePreparationManager未导入，跳过设备预处理")
            else:
                try:
                    device_manager = EnhancedDevicePreparationManager(
                        save_logs=config["save_logs"]
                    )
                    print(f"✅ 设备预处理管理器已加载: {type(device_manager).__name__}")
                except Exception as init_err:
                    print(f"❌ 设备预处理管理器初始化失败: {init_err}")
                    device_manager = None

            if not device_manager:
                print("⚠️ 设备预处理管理器不可用，跳过预处理执行")
            else:
                print("📱 开始设备预处理...")
                def run_usb_check():
                    print("🔍 执行USB连接检查...")
                    if not device_manager._check_usb_connections():
                        print("❌ USB连接检查失败")
                        return False
                    return True

                def run_wireless():
                    print("📶 配置无线连接...")
                    if not device_manager._setup_wireless_connection(
                        self.device.serial
                    ):
                        print("⚠️ 无线连接配置失败，但继续执行")
                    return True

                def run_auto_dialog():
                    print("🛡️ 配置弹窗自动处理...")
                    device_manager._fix_device_permissions(self.device.serial)
                    return True

                def run_screen_lock():
                    print("🔓 处理屏幕锁定...")

                    from screen_state_detector import ScreenStateDetector
                    detector = ScreenStateDetector(self.device.serial)
                    if detector.ensure_screen_ready():
                        print("✅ 智能屏幕检测成功，跳过旧版屏幕处理")
                        return True

                def run_input_method():
                    print("⌨️ 设置输入法...")
                    if not device_manager._wake_up_yousite(self.device.serial):
                        print("⚠️ 输入法设置失败，但继续执行")
                    return True

                operations = [
                    (config["check_usb"], True, run_usb_check),
                    (config["setup_wireless"], False, run_wireless),
                    (config["auto_handle_dialog"], False, run_auto_dialog),
                    (config["handle_screen_lock"], False, run_screen_lock),
                    (config["setup_input_method"], False, run_input_method),
                ]

                for enabled, critical, executor in operations:
                    if not enabled:
                        continue
                    result = executor()
                    if critical and result is False:
                        success = False
                        break

        except Exception as err:
            print(f"❌ 设备预处理过程中出现错误: {err}")
            success = False

        print(f"✅ 设备预处理完成，结果: {'成功' if success else '失败'}")

        # 获取设备截图用于报告展示
        log_dir = None
        if self.log_txt_path:
            log_dir = os.path.dirname(self.log_txt_path)

        # 创建screen对象（内部会自动截取当前屏幕状态）
        screen_data = self._create_unified_screen_object(
            log_dir,
            pos_list=[],
            confidence=1.0,
            rect_info=[]
        )

        timestamp = time.time()
        device_prep_entry = {
            "tag": "function",
            "depth": 1,
            "time": timestamp,
            "data": {
                "name": "device_preparation",
                "call_args": {
                    "device_serial": self.device.serial,
                    "check_usb": config["check_usb"],
                    "setup_wireless": config["setup_wireless"],
                    "auto_handle_dialog": config["auto_handle_dialog"],
                    "handle_screen_lock": config["handle_screen_lock"],
                    "setup_input_method": config["setup_input_method"],
                    "save_logs": config["save_logs"]
                },
                "start_time": timestamp,
                "ret": success,
                "end_time": timestamp + 1.0
            }
        }
        if screen_data:
            device_prep_entry["data"]["screen"] = screen_data

        device_prep_entry["data"]["executed"] = success

        self._write_log_entry(device_prep_entry)
        
        # 标记预处理已执行
        self._device_preparation_executed = True

        return ActionResult(
            success=True,
            message="设备预处理完成",
            details={
                "operation": "device_preparation",
                "cleanup_performed": True
            }
        )

    def _handle_app_start(self, step, step_idx):
        """处理应用启动步骤"""
        print(f"处理应用启动步骤: {step_idx + 1}")
        step_remark = step.get("remark", "")
        app_name = step.get("app_name", "")
        package_name = step.get("package_name", "")        # 扁平化权限配置参数（兼容多种参数名）

        if not package_name:
            print(f"错误: app_start 步骤必须提供 package_name 参数")
            return ActionResult(
                success=False,
                message="app_start 步骤必须提供 package_name 参数",
                details={"operation": "app_start", "error": "missing_package_name"}
            )

        print(f"启动应用: {app_name or package_name} - {step_remark}")


        try:
            # 步骤1: 首先实际启动应用
            app_identifier = app_name or package_name

            print(f"🚀 正在启动应用: {app_identifier}")
            # 使用AppLifecycleManager来实际启动应用
            app_manager = AppLifecycleManager() if AppLifecycleManager else None
            # 现在所有信息都在脚本中提供，直接使用package_name启动
            if package_name and app_manager:
                print(f"🔍 使用脚本中提供的包名直接启动: {package_name}")
                startup_success = app_manager.force_start_by_package(package_name, self.device.serial)
            else:
                print(f"❌ 缺少package_name参数或AppLifecycleManager不可用，无法启动应用")
                startup_success = False
            print(f"应用启动命令执行: {'成功' if startup_success else '失败'}")



            # 记录应用启动日志
            timestamp = time.time()

            # 获取截图目录
            log_dir = None
            if self.log_txt_path:
                log_dir = os.path.dirname(self.log_txt_path)

            # 创建screen对象
            screen_data = self._create_unified_screen_object(
                log_dir,
                pos_list=[],
                confidence=1.0,
                rect_info=[]
            )

            app_start_entry = {
                "tag": "function",
                "depth": 1,
                "time": timestamp,
                "data": {
                    "name": "start_app",
                    "call_args": {
                        "app_name": app_identifier
                        },
                    "start_time": timestamp,
                    "ret": startup_success,
                    "end_time": timestamp + 1
                }
            }
            # 添加screen对象到日志条目（如果可用）
            if screen_data:
                app_start_entry["data"]["screen"] = screen_data            # 添加 executed 字段到日志条目
            app_start_entry["data"]["executed"] = startup_success

            self._write_log_entry(app_start_entry)

            # 修复: 根据实际结果返回正确的状态
            if startup_success:
                print("✅ 应用启动成功")
                return ActionResult(
                    success=True,
                    message="应用启动成功",
                    details={
                        "operation": "app_start",
                        "app_name": app_name,
                        "package_name": package_name
                    }
                )
            else:
                print("❌ 应用启动失败")
                return ActionResult(
                    success=False,
                    message="应用启动失败",
                    details={
                        "operation": "app_start",
                        "error": "startup_failed"
                    }
                )

        except Exception as e:
            print(f"启动应用失败: {e}")
            return ActionResult(
                success=False,
                message=f"启动应用异常: {str(e)}",
                details={"operation": "app_start", "error": str(e)}
            )

    def _handle_app_stop(self, step, step_idx):
        """处理应用停止步骤"""
        action = step.get("action", "")
        step_remark = step.get("remark", "")
        app_name = step.get("app_name", "")
        package_name = step.get("package_name", "")

        print(f"停止应用 - {step_remark}")

        try:
            app_manager = AppLifecycleManager() if AppLifecycleManager else None
            print(f"应用管理器: {app_manager}")

            if package_name and app_manager:
                # 直接使用包名停止应用
                print(f"使用包名停止应用: {package_name}")
                result = app_manager.force_stop_by_package(package_name, self.device.serial)
                call_args = {"package_name": package_name}
            elif app_name and app_manager:
                # 使用模板名停止应用
                print(f"使用模板名停止应用: {app_name}")
                result = app_manager.stop_app(app_name, self.device.serial)
                call_args = {"app_name": app_name}
            else:
                print("错误: 未提供app_name或package_name参数，或AppLifecycleManager不可用")
                return ActionResult(
                    success=False,
                    message="未提供app_name或package_name参数，或AppLifecycleManager不可用",
                    details={"operation": "app_stop", "error": "missing_parameters"}
                )

            print(f"应用停止结果: {result}")

            # 记录应用停止日志
            timestamp = time.time()
            # 获取截图目录
            log_dir = None
            if self.log_txt_path:
                log_dir = os.path.dirname(self.log_txt_path)

            # 创建screen对象
            screen_data = self._create_unified_screen_object(
                log_dir,
                pos_list=[],
                confidence=1.0,
                rect_info=[]
            )

            app_stop_entry = {
                "tag": "function",
                "depth": 1,
                "time": timestamp,
                "data": {
                    "name": "stop_app",
                    "call_args": call_args,
                    "start_time": timestamp,
                    "ret": result,
                    "end_time": timestamp + 1
                }
            }
            # 添加screen对象到日志条目（如果可用）
            if screen_data:
                app_stop_entry["data"]["screen"] = screen_data            # 添加 executed 字段到日志条目
            app_stop_entry["data"]["executed"] = result

            self._write_log_entry(app_stop_entry)

            return ActionResult(
                success=True,
                message=f"应用停止完成: {app_name or package_name}",
                details={
                    "operation": "app_stop",
                    "app_name": app_name,
                    "package_name": package_name,
                    "result": result
                }
            )
        except Exception as e:
            print(f"停止应用失败: {e}")
            return ActionResult(
                success=False,
                message=f"停止应用异常: {str(e)}",
                details={"operation": "app_stop", "error": str(e)}
            )

    def _handle_log(self, step, step_idx):
        """处理日志步骤"""
        log_message = step.get("remark", "")
        print(f"日志: {log_message}")

        # 记录日志条目
        timestamp = time.time()
        log_entry = {
            "tag": "function",
            "depth": 1,
            "time": timestamp,
            "data": {
                "name": "log",
                "call_args": {"msg": log_message},
                "start_time": timestamp,
                "ret": None,
                "end_time": timestamp,
                "executed": True  # 日志步骤必然执行
            }
        }
        self._write_log_entry(log_entry)

        return ActionResult(
            success=True,
            message=f"日志记录完成: {log_message}",
            details={
                "operation": "log",
                "message": log_message
            },
            executed=True
        )

    def _handle_wait_if_exists(self, step, step_idx, log_dir):
        """处理条件等待步骤"""
        element_class = step.get("yolo_class", "")
        step_remark = step.get("remark", "")
        polling_interval = step.get("polling_interval", 5)   # 默认5秒轮询
        max_duration = step.get("max_duration", 300)  # 默认300秒超时
        confidence = step.get("confidence", 0.8)  # 默认置信度

        print(f"\n🚀 [步骤 {step_idx+1}] 开始执行 wait_if_exists 操作")
        print(f"📋 元素类型: '{element_class}'")
        print(f"⚙️ 轮询间隔: {polling_interval}秒")
        print(f"⏰ 最大等待: {max_duration}秒")
        print(f"🎯 置信度: {confidence}")
        print(f"📝 备注: {step_remark}")
        print(f"⏱️ 步骤开始时间: {time.strftime('%H:%M:%S', time.localtime())}")

        wait_start_time = time.time()
        element_found = False
        wait_result = "not_found"  # not_found, disappeared, timeout
        success = False  # 修复：初始化success变量，避免UnboundLocalError

        try:
            # 第一步：检查元素是否存在
            print(f"\n🔍 [阶段1] 检查元素 '{element_class}' 是否存在...")
            # 获取当前屏幕截图
            print(f"📱 正在获取屏幕截图...")
            screenshot = get_device_screenshot(self.device)
            if screenshot is None:
                print(f"❌ 警告: 无法获取屏幕截图，跳过条件等待")
                wait_result = "screenshot_failed"
            else:
                # Convert PIL Image to numpy array to access shape
                screenshot_array = np.array(screenshot)
                screenshot_cv = cv2.cvtColor(screenshot_array, cv2.COLOR_RGB2BGR)

                # 使用传递的检测函数进行AI检测
                if self.detect_buttons:
                    success, detection_result = self.detect_buttons(screenshot_cv, target_class=element_class)
                    print(f"🔍 检测结果: success={success}, detection_result={detection_result}")

                    if success and detection_result[0] is not None:
                        element_found = True
                        # 修复：处理返回4个值的情况 (x, y, class, extra)
                        if len(detection_result) >= 3:
                            x, y, detected_class = detection_result[0], detection_result[1], detection_result[2]
                        else:
                            x, y, detected_class = detection_result[0], detection_result[1], "unknown"
                        print(f"✅ [阶段1-成功] 元素 '{element_class}' 已找到!")
                        print(f"📍 位置: ({x:.1f}, {y:.1f})")
                        print(f"🏷️ 检测类别: {detected_class}")
                    else:
                        element_found = False
                        print(f"❌ [阶段1-失败] 未检测到元素 '{element_class}'")
                else:
                    print(f"⚠️ 检测函数不可用，跳过实际检测")
                    element_found = False

                if element_found:
                    print(f"✅ [阶段1] 元素 '{element_class}' 已存在，开始监控消失...")

                    # 第二步：监控元素消失
                    print(f"\n👁️ [阶段2] 监控元素消失...")
                    loop_count = 0

                    while element_found and (time.time() - wait_start_time) < max_duration:
                        loop_count += 1
                        print(f"🔄 [循环 {loop_count}] 等待元素消失... (已等待 {time.time() - wait_start_time:.1f}秒)")

                        time.sleep(polling_interval)

                        # 重新检测
                        current_screenshot = get_device_screenshot(self.device)
                        if current_screenshot is not None:
                            print(f"🤖 [循环 {loop_count}] 重新检测元素...")
                            current_screenshot_array = np.array(current_screenshot)
                            current_screenshot_cv = cv2.cvtColor(current_screenshot_array, cv2.COLOR_RGB2BGR)

                            # 重新检测元素是否仍然存在
                            if self.detect_buttons:
                                current_success, current_result = self.detect_buttons(current_screenshot_cv, target_class=element_class)
                                print(f"🔍 [循环 {loop_count}] 检测结果: success={current_success}")

                                if current_success:
                                    # 元素仍然存在，继续等待
                                    print(f"⏳ [循环 {loop_count}] 元素仍然存在，继续等待...")
                                else:
                                    element_found = False
                                    wait_result = "disappeared"
                                    elapsed_time = time.time() - wait_start_time
                                    print(f"🎉 [循环 {loop_count}] 元素已消失! 总等待时间: {elapsed_time:.1f}秒")
                            else:
                                current_success = False  # 如果检测函数不可用，假设元素已消失

                            if not current_success:
                                element_found = False
                                wait_result = "disappeared"
                                elapsed_time = time.time() - wait_start_time
                                print(f"🎉 [循环 {loop_count}] 元素已消失! 总等待时间: {elapsed_time:.1f}秒")
                                break
                        else:
                            print(f"❌ [循环 {loop_count}] 无法获取屏幕截图")

                    if element_found and (time.time() - wait_start_time) >= max_duration:
                        wait_result = "timeout"
                        print(f"⏰ [阶段2] 等待超时: 元素在 {max_duration}秒后仍未消失")
                    else:
                        print(f"🎉 元素消失监控完成")
                else:
                    print(f"ℹ️ [阶段1] 元素 '{element_class}' 不存在，无需等待")
                    wait_result = "not_found"

        except Exception as e:
            print(f"❌ wait_if_exists 执行过程中发生异常: {e}")
            traceback.print_exc()
            wait_result = "error"

        # 记录最终结果
        timestamp = time.time()
        total_wait_time = timestamp - wait_start_time

        print(f"\n🏁 [步骤 {step_idx+1}] wait_if_exists 执行完成")
        print(f"📊 最终结果:")
        print(f"   - 元素发现: {element_found}")
        print(f"   - 等待结果: {wait_result}")
        print(f"   - 总等待时间: {total_wait_time:.1f}秒")
        print(f"{'='*60}")        # 创建screen对象以支持报告截图显示
        screen_data = self._create_unified_screen_object(
            log_dir,
            pos_list=[],
            confidence=1.0,
            rect_info=[]
        )

        # 记录条件等待日志
        wait_entry = {
            "tag": "function",
            "depth": 1,
            "time": timestamp,
            "data": {
                "name": "wait_if_exists",
                "call_args": {
                    "element_class": element_class,
                    "polling_interval": polling_interval,
                    "max_duration": max_duration,
                    "confidence": confidence
                },
                "start_time": wait_start_time,
                "ret": {
                    "element_found": element_found,
                    "wait_result": wait_result,
                    "total_wait_time": total_wait_time
                },
                "end_time": timestamp,
                "desc": step_remark or "条件等待操作",
                "title": f"#{step_idx+1} {step_remark or '条件等待操作'}"
            }
        }

        # 添加screen对象到日志条目（如果可用）
        if screen_data:
            wait_entry["data"]["screen"] = screen_data

        # 添加 executed 字段到日志条目
        wait_entry["data"]["executed"] = success

        self._write_log_entry(wait_entry)

        # 返回统一的ActionResult对象
        # wait_if_exists 操作成功的定义：
        # 1. not_found: 元素不存在，操作成功（无需等待）
        # 2. disappeared: 元素存在但已消失，操作成功
        # 3. timeout: 元素存在但超时未消失，操作失败
        # 4. error/screenshot_failed: 发生错误，操作失败
        success = wait_result in ["not_found", "disappeared"]
        message = f"wait_if_exists操作{'成功' if success else '失败'}: {wait_result}"

        return ActionResult(
            success=success,
            message=message,
            details={
                "operation": "wait_if_exists",
                "element_found": element_found,
                "wait_result": wait_result,
                "total_wait_time": total_wait_time,
                "element_class": element_class,
                "confidence": confidence
            }
        )

    def _handle_wait_for_disappearance(self, step, step_idx, log_dir):
        """处理等待消失步骤"""
        element_class = step.get("yolo_class", "")
        step_remark = step.get("remark", "")
        polling_interval = step.get("polling_interval", 1)  # 默认1秒轮询
        max_duration = step.get("max_duration", 30)  # 默认30秒超时
        confidence = step.get("confidence", 0.8)  # 默认置信度

        print(f"\n🚀 [步骤 {step_idx+1}] 开始执行 wait_for_disappearance 操作")
        print(f"📋 元素类型: '{element_class}'")
        print(f"⚙️ 轮询间隔: {polling_interval}秒")
        print(f"⏰ 最大等待: {max_duration}秒")
        print(f"🎯 置信度: {confidence}")
        print(f"📝 备注: {step_remark}")

        wait_start_time = time.time()
        element_disappeared = False
        wait_result = "timeout"  # timeout, disappeared, error
        success = False  # 修复：初始化success变量，避免UnboundLocalError

        try:
            loop_count = 0
            while (time.time() - wait_start_time) < max_duration:
                loop_count += 1
                print(f"🔄 [循环 {loop_count}] 检测元素是否已消失... (已等待 {time.time() - wait_start_time:.1f}秒)")

                # 获取当前屏幕截图
                screenshot = self.device.screenshot()
                if screenshot is None:
                    print(f"❌ [循环 {loop_count}] 无法获取屏幕截图")
                    time.sleep(polling_interval)
                    continue

                # 转换为OpenCV格式
                screenshot_array = np.array(screenshot)
                screenshot_cv = cv2.cvtColor(screenshot_array, cv2.COLOR_RGB2BGR)

                # 使用检测函数进行实际检测
                if self.detect_buttons:
                    success, detection_result = self.detect_buttons(screenshot_cv, target_class=element_class)
                    element_found = success and detection_result[0] is not None
                    print(f"🔍 [循环 {loop_count}] 检测结果: success={success}, 元素存在={element_found}")
                else:
                    # 如果检测函数不可用，假设元素已消失
                    element_found = False
                    print(f"⚠️ [循环 {loop_count}] 检测函数不可用，假设元素已消失")

                if not element_found:
                    element_disappeared = True
                    wait_result = "disappeared"
                    elapsed_time = time.time() - wait_start_time
                    print(f"🎉 [循环 {loop_count}] 元素已消失! 总等待时间: {elapsed_time:.1f}秒")
                    break
                else:
                    print(f"⏳ [循环 {loop_count}] 元素仍然存在，继续等待...")

                time.sleep(polling_interval)

        except Exception as e:
            print(f"❌ wait_for_disappearance 执行过程中发生异常: {e}")
            traceback.print_exc()
            wait_result = "error"

        # 记录最终结果
        timestamp = time.time()
        total_wait_time = timestamp - wait_start_time

        print(f"\n🏁 [步骤 {step_idx+1}] wait_for_disappearance 执行完成")
        print(f"📊 最终结果:")
        print(f"   - 元素已消失: {element_disappeared}")
        print(f"   - 等待结果: {wait_result}")
        print(f"   - 总等待时间: {total_wait_time:.1f}秒")
        print(f"{'='*60}")

        # 创建screen对象以支持报告截图显示
        screen_data = self._create_unified_screen_object(
            log_dir,
            pos_list=[],
            confidence=1.0,
            rect_info=[]
        )


        # 记录条件等待日志
        wait_entry = {
            "tag": "function",
            "depth": 1,
            "time": timestamp,
            "data": {
                "name": "wait_for_disappearance",
                "call_args": {
                    "element_class": element_class,
                    "polling_interval": polling_interval,
                    "max_duration": max_duration,
                    "confidence": confidence
                },
                "start_time": wait_start_time,
                "ret": {
                    "element_disappeared": element_disappeared,
                    "wait_result": wait_result,
                    "total_wait_time": total_wait_time
                },
                "end_time": timestamp,
                "desc": step_remark or "等待消失操作",
                "title": f"#{step_idx+1} {step_remark or '等待消失操作'}"
            }
        }        # 添加screen对象到日志条目（如果可用）
        if screen_data:
            wait_entry["data"]["screen"] = screen_data

        # 添加 executed 字段到日志条目
        wait_entry["data"]["executed"] = success

        self._write_log_entry(wait_entry)

        # 返回统一的ActionResult对象
        success = element_disappeared and wait_result == "disappeared"
        message = f"wait_for_disappearance操作{'成功' if success else '失败'}: {wait_result}"

        return ActionResult(
            success=success,
            message=message,
            details={
                "operation": "wait_for_disappearance",
                "element_disappeared": element_disappeared,
                "wait_result": wait_result,
                "total_wait_time": total_wait_time,
                "element_class": element_class,
                "confidence": confidence
            }
        )

    def _handle_swipe(self, step, step_idx):
        """处理滑动步骤"""
        start_x = step.get("start_x")
        start_y = step.get("start_y")
        end_x = step.get("end_x")
        end_y = step.get("end_y")
        duration = step.get("duration", 300)
        step_remark = step.get("remark", "")

        if start_x is None or start_y is None or end_x is None or end_y is None:
            print(f"错误: swipe 步骤缺少必要的坐标参数")
            return ActionResult(
                success=False,
                message="swipe 步骤缺少必要的坐标参数",
                details={"operation": "swipe", "error": "missing_coordinates"}
            )
        print(f"执行滑动操作: ({start_x}, {start_y}) -> ({end_x}, {end_y}), 持续{duration}ms: {step_remark}")

        # 获取截图目录
        log_dir = None
        if self.log_txt_path:
            log_dir = os.path.dirname(self.log_txt_path)

        # 执行ADB滑动命令
        self.device.shell(f"input swipe {int(start_x)} {int(start_y)} {int(end_x)} {int(end_y)} {int(duration)}")

        # 创建screen对象以支持报告截图显示
        screen_data = self._create_unified_screen_object(
            log_dir,
            pos_list=[[int(start_x), int(start_y)], [int(end_x), int(end_y)]],
            confidence=1.0,
            rect_info=[{
                "left": min(int(start_x), int(end_x)) - 20,
                "top": min(int(start_y), int(end_y)) - 20,
                "width": abs(int(end_x) - int(start_x)) + 40,
                "height": abs(int(end_y) - int(start_y)) + 40
            }]
        )

        # 记录滑动日志
        timestamp = time.time()
        swipe_entry = {
            "tag": "function",
            "depth": 1,
            "time": timestamp,
            "data": {
                "name": "swipe",
                "call_args": {
                    "start": [int(start_x), int(start_y)],
                    "end": [int(end_x), int(end_y)],
                    "duration": int(duration)
                },
                "start_time": timestamp,
                "ret": {
                    "start_pos": [int(start_x), int(start_y)],
                    "end_pos": [int(end_x), int(end_y)]
                },
                "end_time": timestamp + (duration / 1000.0),
                "desc": step_remark or "滑动操作",
                "title": f"#{step_idx+1} {step_remark or '滑动操作'}"
            }
        }

        # 添加screen对象到日志条目（如果可用）
        if screen_data:
            swipe_entry["data"]["screen"] = screen_data        # 添加 executed 字段到日志条目
        swipe_entry["data"]["executed"] = True

        self._write_log_entry(swipe_entry)

        # 滑动后等待一段时间让UI响应
        time.sleep(duration / 1000.0 + 0.5)

        return ActionResult(
            success=True,
            message=f"滑动操作完成: ({start_x}, {start_y}) -> ({end_x}, {end_y})",
            details={
                "operation": "swipe",
                "start_position": (start_x, start_y),
                "end_position": (end_x, end_y),
                "duration": duration,
                "has_screenshot": screen_data is not None
            }
        )

    def _handle_input(self, step, step_idx):
        """处理文本输入步骤"""
        input_text = step.get("text", "")
        target_selector = step.get("target_selector", {})
        step_remark = step.get("remark", "")

        # 智能账号分配：如果需要账号参数但没有分配，尝试自动分配
        if ("${account:username}" in input_text or "${account:password}" in input_text):
            if not self.device_account:
                print("🔄 检测到需要账号参数但设备未分配账号，尝试自动分配...")
                self._auto_allocate_device_account()

        # 参数替换处理：${account:username} 和 ${account:password}
        if "${account:username}" in input_text:
            if self.device_account and len(self.device_account) >= 1:
                input_text = input_text.replace("${account:username}", self.device_account[0])
                print(f"✅ 替换用户名参数: {self.device_account[0]}")
            else:
                device_serial = getattr(self.device, 'serial', self.device_name)
                print(f"❌ 错误: 设备 {device_serial} 没有分配账号，无法替换用户名参数")
                return True, False, True

        if "${account:password}" in input_text:
            if self.device_account and len(self.device_account) >= 2:
                input_text = input_text.replace("${account:password}", self.device_account[1])
                print(f"✅ 替换密码参数")
            else:
                device_serial = getattr(self.device, 'serial', self.device_name)
                print(f"❌ 错误: 设备 {device_serial} 没有分配账号，无法替换密码参数")
                return True, False, True
            print(f"执行文本输入 - {step_remark}")
        try:
            # 获取截图目录
            log_dir = None
            if self.log_txt_path:
                log_dir = os.path.dirname(self.log_txt_path)

            # 初始化增强输入处理器
            if DeviceScriptReplayer:
                input_handler = DeviceScriptReplayer(self.device.serial)

                # 检查是否使用智能参数化选择器
                if target_selector.get('type'):
                    print(f"🤖 使用智能参数化输入: type={target_selector.get('type')}")
                    # 先查找目标输入框
                    target_element = input_handler.find_element_smart(target_selector)
                    if target_element:
                        print(f"✅ 找到目标输入框: {target_element.get('text', '')[:20]}...")
                        # 点击获取焦点后输入文本
                        if input_handler.tap_element(target_element):
                            success = input_handler.input_text_smart(input_text)
                        else:
                            print("❌ 点击输入框获取焦点失败")
                            success = False
                    else:
                        print("❌ 未找到匹配的输入框元素")
                        success = False
                else:
                    # 传统方式：使用增强版焦点检测
                    success = input_handler.input_text_with_focus_detection(input_text, target_selector)
            else:
                print("⚠️ DeviceScriptReplayer不可用，无法执行文本输入")
                return True, False, True

            if success:
                print(f"✅ 文本输入成功")

                # 创建screen对象以支持报告截图显示
                screen_data = self._create_unified_screen_object(
                    log_dir,
                    pos_list=[],
                    confidence=1.0,
                    rect_info=[]
                )

                # 记录输入操作日志
                timestamp = time.time()
                input_entry = {
                    "tag": "function",
                    "depth": 1,
                    "time": timestamp,
                    "data": {
                        "name": "input_text",
                        "call_args": {
                    "tag": "function",
                    "depth": 1,
                    "time": timestamp,
                    "data": {
                        "name": "input_text",
                        "call_args": {
                            "text": "***" if "${account:password}" in step.get("text", "") else input_text,
                            "target_selector": target_selector
                        },
                        "start_time": timestamp,
                        "ret": {"success": True},
                        "end_time": timestamp + 1,
                        "desc": step_remark or "文本输入操作",
                        "title": f"#{step_idx+1} {step_remark or '文本输入操作'}"
                    }
                }
                   }
                }
                # 添加screen对象到日志条目（如果可用）
                if screen_data:
                    input_entry["data"]["screen"] = screen_data

                # 添加 executed 字段到日志条目
                input_entry["data"]["executed"] = True

                self._write_log_entry(input_entry)

                return ActionResult(
                    success=True,
                    message="文本输入完成",
                    details={
                        "operation": "input",
                        "text_masked": "***" if "${account:password}" in step.get("text", "") else input_text,
                        "has_screenshot": screen_data is not None
                    }
                )
            else:
                print(f"❌ 错误: 文本输入失败 - 无法找到合适的输入焦点")
                return ActionResult(
                    success=False,
                    message="文本输入失败 - 无法找到合适的输入焦点",
                    details={"operation": "input", "error": "no_input_focus"}
                )

        except Exception as e:
            print(f"❌ 错误: 文本输入过程中发生异常: {e}")
            traceback.print_exc()
            return ActionResult(
                success=False,
                message=f"文本输入异常: {str(e)}",
                details={"operation": "input", "error": str(e)}
            )

    def _handle_checkbox(self, step, step_idx):
        """处理checkbox勾选步骤"""
        target_selector = step.get("target_selector", {})
        step_remark = step.get("remark", "")

        print(f"执行checkbox勾选操作 - {step_remark}")
        try:
            # 获取截图目录
            log_dir = None
            if self.log_txt_path:
                log_dir = os.path.dirname(self.log_txt_path)

            # 初始化增强输入处理器
            if DeviceScriptReplayer:
                input_handler = DeviceScriptReplayer(self.device.serial)

                # 获取UI结构
                xml_content = input_handler.get_ui_hierarchy()
                if xml_content:
                    elements = input_handler._parse_ui_xml(xml_content)
                    # 查找checkbox - 使用智能查找方法
                    if target_selector.get('type'):
                        # 新版：使用智能元素查找
                        checkbox = input_handler.find_element_smart(target_selector)
                    else:
                        # 传统方式：使用具体的CHECKBOX_PATTERNS
                        checkbox = input_handler.find_agreement_checkbox(elements, target_selector)

                    if checkbox:
                        success = input_handler.check_checkbox(checkbox)

                        if success:
                            print(f"✅ checkbox勾选成功")

                            # 创建screen对象以支持报告截图显示
                            screen_data = self._create_unified_screen_object(
                                log_dir,
                                pos_list=[],
                                confidence=1.0,
                                rect_info=[]
                            )

                            # 记录checkbox操作日志
                            timestamp = time.time()
                            checkbox_entry = {
                                "tag": "function",
                                "depth": 1,
                                "time": timestamp,
                                "data": {
                                    "name": "check_checkbox",
                                    "call_args": {
                                        "target_selector": target_selector
                                    },
                                    "start_time": timestamp,
                                    "ret": {"success": True},
                                    "end_time": timestamp + 0.5,
                                    "desc": step_remark or "勾选checkbox操作",
                                    "title": f"#{step_idx+1} {step_remark or '勾选checkbox操作'}"
                                }
                            }

                            # 添加screen对象到日志条目（如果可用）
                            if screen_data:
                                checkbox_entry["data"]["screen"] = screen_data

                            # 添加 executed 字段到日志条目
                            checkbox_entry["data"]["executed"] = success

                            self._write_log_entry(checkbox_entry)

                            return ActionResult(
                                success=True,
                                message="checkbox勾选成功",
                                details={
                                    "operation": "checkbox",
                                    "has_screenshot": screen_data is not None
                                }
                            )
                        else:
                            print(f"❌ 错误: checkbox勾选失败")
                            return ActionResult(
                                success=False,
                                message="checkbox勾选失败",
                                details={"operation": "checkbox", "error": "click_failed"}
                            )
                    else:
                        print(f"❌ 错误: 未找到checkbox元素")
                        return ActionResult(
                            success=False,
                            message="未找到checkbox元素",
                            details={"operation": "checkbox", "error": "element_not_found"}
                        )
                else:
                    print(f"❌ 错误: 未找到checkbox元素")
                    return ActionResult(
                        success=False,
                        message="未找到checkbox元素",
                        details={"operation": "checkbox", "error": "element_not_found"}
                    )
            else:
                print(f"❌ 错误: 无法获取UI结构")
                return ActionResult(
                    success=False,
                    message="无法获取UI结构",
                    details={"operation": "checkbox", "error": "ui_hierarchy_unavailable"}
                )

        except Exception as e:
            print(f"❌ 错误: checkbox勾选过程中发生异常: {e}")
            traceback.print_exc()
            return ActionResult(
                success=False,
                message=f"checkbox勾选异常: {str(e)}",
                details={"operation": "checkbox", "error": str(e)}
            )

    def _create_unified_screen_object(self, log_dir, pos_list=None, confidence=0.85, rect_info=None):
        """
        创建统一的screen对象 - 增强版
        🔧 修复: 即使截图失败也返回基本的screen对象
        """
        try:
            if not log_dir:
                print("⚠️ 警告: log_dir未设置，无法创建screen对象")
                return None

            # 🔧 移除: 不再检查multi_device_replay和创建新目录，直接使用传入的log_dir
            # 确保log_dir存在
            os.makedirs(log_dir, exist_ok=True)

            # 生成时间戳文件名
            timestamp = time.time()
            screenshot_timestamp = int(timestamp * 1000)
            screenshot_filename = f"{screenshot_timestamp}.jpg"
            thumbnail_filename = f"{screenshot_timestamp}_small.jpg"

            # 设置路径 - 直接在log_dir下
            screenshot_path = os.path.join(log_dir, screenshot_filename)
            thumbnail_path = os.path.join(log_dir, thumbnail_filename)

            # 设置相对路径 - 直接使用文件名
            screenshot_relative = screenshot_filename
            thumbnail_relative = thumbnail_filename

            # 获取设备截图
            screenshot_success = False
            resolution = [1080, 2400]  # 默认分辨率

            try:
                screenshot = get_device_screenshot(self.device)
                if screenshot:
                    # 转换为OpenCV格式
                    import cv2
                    import numpy as np
                    frame = cv2.cvtColor(np.array(screenshot), cv2.COLOR_RGB2BGR)

                    # 保存截图
                    cv2.imwrite(screenshot_path, frame, [cv2.IMWRITE_JPEG_QUALITY, 85])

                    # 创建缩略图
                    small_frame = cv2.resize(frame, (0, 0), fx=0.3, fy=0.3)
                    cv2.imwrite(thumbnail_path, small_frame, [cv2.IMWRITE_JPEG_QUALITY, 60])

                    # 获取实际分辨率
                    height, width = frame.shape[:2]
                    resolution = [width, height]
                    screenshot_success = True

                    print(f"✅ 截图保存成功: {screenshot_path}")
                    # 记录路径供后续步骤结果使用
                    try:
                        self._last_screenshot_path = screenshot_path
                    except Exception:
                        pass

                else:
                    print("⚠️ 截图获取失败，使用默认screen对象")
                    try:
                        self._last_screenshot_path = None
                    except Exception:
                        pass

            except Exception as e:
                print(f"⚠️ 截图处理失败: {e}")
                try:
                    self._last_screenshot_path = None
                except Exception:
                    pass

            # 🔧 修复: 即使截图失败也创建screen对象，直接使用文件名
            screen_object = {
                "src": screenshot_relative,
                "_filepath": screenshot_path,
                "thumbnail": thumbnail_relative,
                "resolution": resolution,
                "pos": pos_list or [],
                "confidence": confidence,
                "rect": rect_info or [],
                "screenshot_success": screenshot_success
            }

            return screen_object

        except Exception as e:
            print(f"❌ _create_unified_screen_object失败: {e}")
            # 返回基本的screen对象，确保日志结构完整

            return {
                "src": "fallback_screenshot.jpg",
                "_filepath": "fallback_screenshot.jpg",
                "thumbnail": "fallback_thumbnail.jpg",
                "resolution": [1080, 2400],
                "pos": pos_list or [],
                "confidence": confidence,
                "rect": rect_info or [],
                "screenshot_success": False
            }

    def _handle_wait_for_appearance(self, step, step_idx, log_dir):
        """处理等待元素出现步骤 - 使用AI检测等待指定元素出现"""
        # 解析参数
        yolo_class = step.get("yolo_class", "")
        step_remark = step.get("remark", "")
        max_duration = step.get("max_duration", 10)
        polling_interval = step.get("polling_interval", 1)
        confidence = step.get("confidence", 0.8)
        fail_on_timeout = step.get("fail_on_timeout", True)
        # OCR 相关参数
        ocr_keywords = step.get("ocr_keywords")
        ocr_min_score = step.get("ocr_min_score", 0.5)
        ocr_match_method = step.get("ocr_match_method", "best")
        ocr_match_method_desc = step.get("ocr_match_method_desc", "")

        # 检测模式（yolo_only | ocr_only | yolo_then_ocr）
        detection_mode = self._determine_detection_mode(step)

        device_serial = getattr(self.device, 'serial', 'Unknown')
        if detection_mode == "ocr_only":
            print(f"  ⏳ 等待元素出现(纯OCR): '{ocr_keywords}'")
        elif detection_mode == "yolo_then_ocr":
            print(
                f"  ⏳ 等待元素出现(YOLO+OCR): 类别='{yolo_class}', 关键字='{ocr_keywords}'"
            )
        else:
            # 默认/兼容：按 YOLO-only 打印
            print(f"  ⏳ 等待元素出现: '{yolo_class}'")
        print(
            f"  ⏰ 最大等待: {max_duration}秒 | 轮询间隔: {polling_interval}秒 | 置信度: {confidence}"
        )
        if step_remark:
            print(f"  📝 备注: {step_remark}")

        wait_start_time = time.time()
        element_appeared = False
        wait_result = "not_appeared"
        detected_class = ""
        detection_result = None
        success = False  # 修复：初始化success变量，避免UnboundLocalError
        
        # 修复：初始化所有在异常处理后使用的变量，避免UnboundLocalError
        click_position = None
        click_success = None
        click_error = None
        execute_action = step.get("execute_action", "click")

        try:
            loop_count = 0
            while time.time() - wait_start_time < max_duration:
                loop_count += 1
                print(f"  🔍 [轮询 {loop_count}] 检测元素...")
                
                if not self.detect_buttons:
                    print("  ❌ AI检测功能不可用")
                    break
                
                # 获取当前屏幕截图
                screenshot = get_device_screenshot(self.device)
                if screenshot is None:
                    print("  ⚠️ 无法获取屏幕截图")
                    time.sleep(polling_interval)
                    continue

                import cv2
                import numpy as np
                frame = cv2.cvtColor(np.array(screenshot), cv2.COLOR_RGB2BGR)

                # 根据模式进行检测
                if detection_mode == "ocr_only":
                    # 纯 OCR 全屏检测
                    if ocr_match_method == "first":
                        ocr_ok, ocr_res = self._ocr_match_first_strategy(
                            frame, ocr_keywords, ocr_min_score
                        )
                    elif ocr_match_method == "desc":
                        ocr_ok, ocr_res = self._ocr_match_desc_strategy(
                            frame, ocr_keywords, ocr_match_method_desc,
                            ocr_min_score
                        )
                    else:
                        from apps.scripts.replay_script import (
                            perform_fullscreen_ocr_detection,
                        )
                        ocr_ok, ocr_res = perform_fullscreen_ocr_detection(
                            frame,
                            ocr_keywords=ocr_keywords,
                            ocr_min_score=ocr_min_score,
                        )

                    if ocr_ok and ocr_res and "position" in ocr_res:
                        x, y = ocr_res["position"]
                        detection_result = (x, y, "ocr", None)
                        detected_class = ocr_res.get("text", "ocr")
                        element_appeared = True
                        wait_result = "appeared"
                        print(
                            f"  ✅ OCR匹配成功: '{detected_class}' 位置({int(x)},{int(y)})"
                        )
                        break
                elif detection_mode == "yolo_then_ocr":
                    # YOLO 预检 + OCR 匹配
                    success, detection_result = self.detect_buttons(
                        frame,
                        target_class=yolo_class,
                        conf_threshold=confidence,
                        use_ocr=True,
                        ocr_keywords=ocr_keywords,
                        ocr_min_score=ocr_min_score,
                    )
                    if success and detection_result and detection_result[0] is not None:
                        element_appeared = True
                        if len(detection_result) >= 3:
                            x, y, detected_class = (
                                detection_result[0],
                                detection_result[1],
                                detection_result[2],
                            )
                        else:
                            x, y, detected_class = (
                                detection_result[0],
                                detection_result[1],
                                "unknown",
                            )
                        wait_result = "appeared"
                        print(f"  ✅ 元素已出现: 位置({x:.1f}, {y:.1f})")
                        break
                else:
                    # 兼容：YOLO-only
                    success, detection_result = self.detect_buttons(
                        frame, target_class=yolo_class
                    )

                    if success and detection_result[0] is not None:
                        element_appeared = True
                        # 处理返回值 (x, y, class, extra)
                        if len(detection_result) >= 3:
                            x, y, detected_class = (
                                detection_result[0],
                                detection_result[1],
                                detection_result[2],
                            )
                        else:
                            x, y, detected_class = (
                                detection_result[0],
                                detection_result[1],
                                "unknown",
                            )
                        wait_result = "appeared"
                        print(f"  ✅ 元素已出现: 位置({x:.1f}, {y:.1f})")
                        break

                print(f"  ⏳ 元素未出现，{polling_interval}秒后重试...")
                time.sleep(polling_interval)

            total_wait_time = time.time() - wait_start_time

            # ====== 自动点击逻辑实现 ======
            if element_appeared and execute_action == "click":
                try:
                    if detection_result and len(detection_result) >= 2 and detection_result[0] is not None and detection_result[1] is not None:
                        abs_x, abs_y = int(detection_result[0]), int(detection_result[1])
                        print(f"  🤖 自动点击: ({abs_x}, {abs_y})")
                        self.device.shell(f"input tap {abs_x} {abs_y}")
                        click_position = (abs_x, abs_y)
                        click_success = True
                    else:
                        print("  ⚠️ 无有效坐标，无法点击")
                        click_success = False
                except Exception as ce:
                    print(f"  ❌ 点击异常: {ce}")
                    click_success = False
                    click_error = str(ce)

            if element_appeared:
                print(f"  ✅ 检测成功 (耗时{total_wait_time:.1f}s)")
            else:
                wait_result = "timeout"
                print(f"  ⏰ 等待超时 (耗时{total_wait_time:.1f}s)")

        except Exception as e:
            print(f"  ❌ 等待异常: {e}")
            wait_result = "error"
            total_wait_time = time.time() - wait_start_time

        # 创建screen对象以支持报告截图显示
        pos_list = []
        if detection_result and len(detection_result) >= 2 and detection_result[0] is not None and detection_result[1] is not None:
            pos_list = [[int(detection_result[0]), int(detection_result[1])]]

        screen_data = self._create_unified_screen_object(
            log_dir,
            pos_list=pos_list,
            confidence=confidence,
            rect_info=[]
        )

        # 记录等待结果日志
        timestamp = time.time()
        wait_entry = {
            "tag": "function",
            "depth": 1,
            "time": timestamp,
            "data": {
                "name": "wait_for_appearance",
                "call_args": {
                    "yolo_class": yolo_class,
                    "max_duration": max_duration,
                    "polling_interval": polling_interval,
                    "confidence": confidence,
                    "execute_action": execute_action,
                    "detection_mode": detection_mode,
                    "ocr_keywords": ocr_keywords,
                    "ocr_min_score": ocr_min_score,
                    "ocr_match_method": ocr_match_method
                },
                "start_time": wait_start_time,
                "ret": {
                    "element_appeared": element_appeared,
                    "wait_result": wait_result,
                    "total_wait_time": total_wait_time,
                    "detected_class": detected_class,
                    "click_position": click_position,
                    "click_success": click_success,
                    "click_error": click_error
                },
                "end_time": timestamp,
                "desc": step_remark or "等待元素出现操作",
                "title": f"#{step_idx+1} {step_remark or '等待元素出现操作'}"
            }
        }        # 添加screen对象到日志条目
        if screen_data:
            wait_entry["data"]["screen"] = screen_data

        # 返回统一的ActionResult对象
        # 修复：正确判断成功条件
        if execute_action == "click":
            # 有自动点击时：元素出现 AND 点击成功
            success = element_appeared and (click_success is True)
        else:
            # 无自动点击时：仅判断元素是否出现
            success = element_appeared
            
        if not element_appeared and not fail_on_timeout:
            success = True  # 忽略超时失败

        # 添加 executed 字段到日志条目
        wait_entry["data"]["executed"] = success

        self._write_log_entry(wait_entry)

        message = f"wait_for_appearance操作{'成功' if success else '失败'}: {wait_result}"

        return ActionResult(
            success=success,
            message=message,
            details={
                "operation": "wait_for_appearance",
                "element_appeared": element_appeared,
                "wait_result": wait_result,
                "total_wait_time": total_wait_time,
                "detected_class": detected_class,
                "yolo_class": yolo_class,
                "click_position": click_position,
                "click_success": click_success,
                "click_error": click_error
            }
        )

    def _handle_wait_for_stable(self, step, step_idx, log_dir):
        """处理等待界面稳定步骤 - 使用截图比较等待界面连续N秒无变化"""
        step_remark = step.get("remark", "")
        stable_duration = step.get("duration", 2)
        max_duration = step.get("max_duration", 10)
        check_structure = step.get("check_structure", True)
        check_positions = step.get("check_positions", True)
        tolerance = step.get("tolerance", 0.05)
        ignore_animations = step.get("ignore_animations", True)

        print(f"\n🚀 [步骤 {step_idx+1}] 开始执行 wait_for_stable 操作")
        print(f"📋 检测方式: {detection_method}")
        print(f"🎯 稳定持续时间: {stable_duration}秒")
        print(f"⏰ 最大等待时间: {max_duration}秒")
        print(f"🔍 检查结构稳定: {check_structure}")
        print(f"📍 检查位置稳定: {check_positions}")
        print(f"📊 变化容忍度: {tolerance}")
        print(f"📝 备注: {step_remark}")

        wait_start_time = time.time()
        stable_start_time = None
        last_screenshot = None
        last_ui_structure = None
        is_stable = False
        stability_result = "not_stable"

        try:
            while time.time() - wait_start_time < max_duration:
                current_time = time.time()

                # 使用截图比较检测稳定性
                current_screenshot = None
                try:
                    import subprocess
                    screenshot_result = subprocess.run(
                        f"adb -s {self.device.serial} exec-out screencap -p",
                        shell=True, capture_output=True
                    )
                    if screenshot_result.returncode == 0:
                        import cv2
                        import numpy as np
                        nparr = np.frombuffer(screenshot_result.stdout, np.uint8)
                        current_screenshot = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
                except Exception as e:
                    print(f"  ⚠️ 截图获取失败: {e}")

                # 检查是否与上次状态相同
                is_same = False
                if current_screenshot is not None:
                    if last_screenshot is not None:
                        # 计算图像差异
                        diff = cv2.absdiff(current_screenshot, last_screenshot)
                        diff_ratio = np.sum(diff) / (diff.shape[0] * diff.shape[1] * diff.shape[2] * 255)
                        is_same = diff_ratio < tolerance
                        print(f"  🖼️ 差异比例: {diff_ratio:.4f}")
                    else:
                        print("  📸 获取参考截图")
                    last_screenshot = current_screenshot.copy()

                # 更新稳定状态
                if is_same:
                    if stable_start_time is None:
                        stable_start_time = current_time
                        print(f"  🟢 开始稳定...")

                    stable_duration = current_time - stable_start_time
                    print(f"  ⏱️ 已稳定 {stable_duration:.1f}/{stable_duration}s")

                    if stable_duration >= stable_duration:
                        is_stable = True
                        stability_result = "stable"
                        print(f"  ✅ 界面稳定 {stable_duration}s!")
                        break
                else:
                    if stable_start_time is not None:
                        print(f"  🔄 界面变化，重新计时")
                    stable_start_time = None

                time.sleep(0.5)  # 检查间隔

            total_wait_time = time.time() - wait_start_time

            if not is_stable:
                stability_result = "timeout"
                print(f"  ⏰ 等待超时 (耗时{total_wait_time:.1f}s)")
            else:
                print(f"  ✅ 稳定检测成功 (耗时{total_wait_time:.1f}s)")

        except Exception as e:
            print(f"  ❌ 稳定检测异常: {e}")
            stability_result = "error"
            total_wait_time = time.time() - wait_start_time

        # 创建screen对象
        screen_data = self._create_unified_screen_object(
            log_dir,
            pos_list=[],
            confidence=1.0,
            rect_info=[]
        )

        # 记录稳定检测结果日志
        timestamp = time.time()
        stable_entry = {
            "tag": "function",
            "depth": 1,
            "time": timestamp,
            "data": {
                "name": "wait_for_stable",
                "call_args": {
                    "stable_duration": stable_duration,
                    "max_duration": max_duration,
                    "tolerance": tolerance
                },
                "start_time": wait_start_time,
                "ret": {
                    "is_stable": is_stable,
                    "stability_result": stability_result,
                    "total_wait_time": total_wait_time
                },
                "end_time": timestamp,
                "desc": step_remark or "等待界面稳定操作",
                "title": f"#{step_idx+1} {step_remark or '等待界面稳定操作'}"
            }
        }        # 添加screen对象到日志条目
        if screen_data:
            stable_entry["data"]["screen"] = screen_data        # 添加 executed 字段到日志条目
        stable_entry["data"]["executed"] = is_stable

        self._write_log_entry(stable_entry)

        return ActionResult(
            success=is_stable,
            message=f"wait_for_stable操作{'成功' if is_stable else '失败'}: {stability_result}",
            details={
                "operation": "wait_for_stable",
                "is_stable": is_stable,
                "stability_result": stability_result,
                "total_wait_time": total_wait_time,
                "stable_duration": stable_duration,
                "max_duration": max_duration
            }
        )

    def _handle_retry_until_success(self, step, step_idx, log_dir):
        """处理重试直到成功步骤 - 使用AI检测对任意操作进行重试"""
        # 解析参数
        execute_action = step.get("execute_action",  "click")
        yolo_class = step.get("yolo_class",  "")
        text = step.get("text",  "")
        step_remark = step.get("remark", "")

        max_retries = step.get("max_retries", 5)
        retry_interval = step.get("retry_interval", 1)
        verify_success = step.get("verify_success", False)
        stop_on_success = step.get("stop_on_success", True)
        
        print(f"  🔄 重试直到成功: {execute_action}操作")
        if yolo_class:
            print(f"  🎯 目标元素: '{yolo_class}'")
        print(f"  🔢 最大重试: {max_retries}次 | 间隔: {retry_interval}秒")
        if step_remark:
            print(f"  📝 备注: {step_remark}")

        retry_start_time = time.time()
        success = False
        last_error = None
        retry_count = 0
        for attempt in range(max_retries + 1):  # +1 为第一次尝试
            retry_count = attempt
            print(f"  🔄 [尝试 {attempt + 1}/{max_retries + 1}] {execute_action}...")

            try:
                operation_success = False
                if execute_action == "click":
                    # AI检测点击
                    if not self.detect_buttons:
                        print("  ❌ AI检测功能不可用")
                        break
                        
                    screenshot = get_device_screenshot(self.device)
                    if screenshot is None:
                        print("  ❌ 无法获取截图")
                        continue

                    import cv2
                    import numpy as np
                    frame = cv2.cvtColor(np.array(screenshot), cv2.COLOR_RGB2BGR)

                    ai_success, detection_result = self.detect_buttons(frame, target_class=yolo_class)
                    if ai_success and detection_result[0] is not None:
                        if len(detection_result) >= 3:
                            x, y, detected_class = detection_result[0], detection_result[1], detection_result[2]
                        else:
                            x, y, detected_class = detection_result[0], detection_result[1], "unknown"
                        self.device.shell(f"input tap {int(x)} {int(y)}")
                        operation_success = True
                        print(f"  ✅ 点击成功: ({x:.1f}, {y:.1f})")

                elif execute_action == "input":
                    # AI定位输入框并输入文本
                    input_text = text

                    # 参数替换处理
                    if "${account:username}" in input_text or "${account:password}" in input_text:
                        if not self.device_account:
                            print("  🔄 自动分配账号...")
                            self._auto_allocate_device_account()

                    if "${account:username}" in input_text:
                        if self.device_account and len(self.device_account) >= 1:
                            input_text = input_text.replace("${account:username}", self.device_account[0])
                            print(f"  ✅ 替换用户名")
                        else:
                            print(f"  ❌ 未分配账号")
                            continue

                    if "${account:password}" in input_text:
                        if self.device_account and len(self.device_account) >= 2:
                            input_text = input_text.replace("${account:password}", self.device_account[1])
                            print(f"  ✅ 替换密码")
                        else:
                            print(f"  ❌ 未分配账号")
                            continue

                    # AI定位输入框
                    if not self.detect_buttons:
                        print("  ❌ AI检测功能不可用")
                        break
                        
                    screenshot = get_device_screenshot(self.device)
                    if screenshot is None:
                        print("  ❌ 无法获取截图")
                        continue
                        
                    import cv2
                    import numpy as np
                    frame = cv2.cvtColor(np.array(screenshot), cv2.COLOR_RGB2BGR)
                    ai_ok, detection_result = self.detect_buttons(frame, target_class=yolo_class)
                    if ai_ok and detection_result[0] is not None:
                        x, y = detection_result[0], detection_result[1]
                        # 点击输入框聚焦
                        self.device.shell(f"input tap {int(x)} {int(y)}")
                        time.sleep(0.5)
                        # 输入文本
                        escaped_text = input_text.replace(' ', '%s').replace("'", "\\'")
                        self.device.shell(f"input text '{escaped_text}'")
                        operation_success = True
                        print(f"  ✅ 文本输入成功")

                # 验证操作成功（如果启用）
                if operation_success and verify_success:
                    print("🔍 验证操作结果...")
                    time.sleep(1)  # 等待UI响应
                    # 可以在这里添加更复杂的验证逻辑
                    # 目前简单假设操作成功

                if operation_success:
                    success = True
                    print(f"  🎉 操作成功!")
                    if stop_on_success:
                        break
                else:
                    print(f"  ❌ 操作失败")

            except Exception as e:
                print(f"  ❌ 操作异常: {e}")
                last_error = str(e)

            # 如果还有重试机会，等待后重试
            if attempt < max_retries and not (success and stop_on_success):
                print(f"  ⏳ 等待{retry_interval}s后重试...")
                time.sleep(retry_interval)

        total_retry_time = time.time() - retry_start_time

        if success:
            print(f"  ✅ 重试成功 (尝试{retry_count + 1}次, 耗时{total_retry_time:.1f}s)")
        else:
            print(f"  ❌ 重试失败 (尝试{retry_count + 1}次, 耗时{total_retry_time:.1f}s)")

        # 创建screen对象
        screen_data = self._create_unified_screen_object(
            log_dir,
            pos_list=[],
            confidence=1.0,
            rect_info=[]
        )

        # 记录重试结果日志
        timestamp = time.time()
        retry_entry = {
            "tag": "function",
            "depth": 1,
            "time": timestamp,
            "data": {
                "name": "retry_until_success",
                "call_args": {
                    "execute_action": execute_action,
                    "yolo_class": yolo_class,
                    "max_retries": max_retries
                },
                "start_time": retry_start_time,
                "ret": {
                    "success": success,
                    "retry_count": retry_count + 1,
                    "total_retry_time": total_retry_time,
                    "last_error": last_error
                },
                "end_time": timestamp,
                "desc": step_remark or "重试直到成功操作",
                "title": f"#{step_idx+1} {step_remark or '重试直到成功操作'}"
            }
        }        # 添加screen对象到日志条目
        if screen_data:
            retry_entry["data"]["screen"] = screen_data

        # 添加 executed 字段到日志条目
        retry_entry["data"]["executed"] = success

        self._write_log_entry(retry_entry)

        return ActionResult(
            success=success,
            message=f"retry_until_success操作{'成功' if success else '失败'}，共重试{retry_count}次",
            details={
                "operation": "retry_until_success",
                "final_success": success,
                "retry_count": retry_count,
                "total_retry_time": total_retry_time,
                "last_error": last_error,
                "execute_action": execute_action
            }
        )

    def _write_log_entry(self, log_entry):
        """Write log entry to log file - 增强版"""
        try:
            # 🔧 修复: 更严格的日志写入验证
            if not self.log_txt_path:
                print(f"⚠️ 警告: log_txt_path未设置，无法写入日志")
                return False

            print(f"🔍 调试: 准备写入日志到: {self.log_txt_path}")
            # print(f"🔍 调试: 日志条目: {log_entry}")

            log_dir = os.path.dirname(self.log_txt_path)
            if not os.path.exists(log_dir):
                print(f"⚠️ 警告: 日志目录不存在，尝试创建: {log_dir}")
                os.makedirs(log_dir, exist_ok=True)

            # 写入日志条目
            with open(self.log_txt_path, "a", encoding="utf-8") as f:
                log_entry_str = json.dumps(log_entry, ensure_ascii=False, separators=(',', ':'))
                f.write(log_entry_str + "\n")
                f.flush()  # 强制刷新缓冲区

            # 验证写入
            if os.path.exists(self.log_txt_path):
                with open(self.log_txt_path, "r", encoding="utf-8") as f:
                    content = f.read()
                print(f"🔍 调试: 文件大小: {len(content)} 字符")
                print(f"📝 日志条目已写入: {log_entry.get('data', {}).get('name', 'unknown')}")
            else:
                print(f"❌ 警告: 写入后文件不存在: {self.log_txt_path}")
                return False

            return True

        except Exception as e:
            print(f"❌ 写入日志失败: {e}")
            import traceback
            traceback.print_exc()
            return False

    def _route_to_action_processor(self, step, step_idx, action_name):
        """
        路由复杂操作到ActionProcessor进行处理

        Args:
            step: 步骤配置
            step_idx: 步骤索引
            action_name: 动作名称

        Returns:
            操作是否成功
        """
        try:
            # 导入ActionProcessor
            try:
                from action_processor import ActionProcessor
            except ImportError:
                from .action_processor import ActionProcessor
                # 在路由前处理参数替换
            step_copy = step.copy()
            # 对于retry_until_success中的input操作，需要特殊处理参数替换
            if action_name == "retry_until_success" and step_copy.get("execute_action") == "input":
                if "text" in step_copy:
                    if DeviceScriptReplayer is not None:
                        try:
                            # 创建实例来调用实例方法
                            device_serial = getattr(self.device, 'serial', None)
                            if device_serial:
                                temp_handler = DeviceScriptReplayer(device_serial)
                                step_copy["text"] = temp_handler._replace_account_parameters(step_copy["text"])
                                print(f"🔧 retry_until_success参数替换完成: {step_copy['text']}")
                            else:
                                print("⚠️ 无法获取设备序列号，跳过参数替换")
                        except Exception as e:
                            print(f"⚠️ 参数替换失败: {e}")
                    else:
                        print("⚠️ DeviceScriptReplayer 不可用，跳过参数替换")            # 🔧 修复: 直接使用设备报告目录，不创建额外的log子目录
            import tempfile
            import os

            # 初始化变量
            temp_log_dir = None

            if hasattr(self, 'log_txt_path') and self.log_txt_path:
                # 获取设备报告目录（log.txt的父目录）
                device_report_dir = os.path.dirname(self.log_txt_path)
                log_dir = device_report_dir  # 直接使用设备报告目录
                log_txt_path = self.log_txt_path  # 使用已设置的路径
                print(f"🔍 调试: 使用设备报告日志路径: {log_txt_path}")
            else:
                # 回退到临时目录（用于兼容性）
                temp_log_dir = tempfile.mkdtemp(prefix=f"enhanced_handler_{action_name}_")
                log_dir = temp_log_dir
                log_txt_path = os.path.join(temp_log_dir, "log.txt")
                print(f"🔍 调试: 使用临时日志路径: {log_txt_path}")
            # 创建一个简单的设备代理对象

            class DeviceProxy:
                def __init__(self, device_serial):
                    self.serial = device_serial

                def screenshot(self):
                    # 通过adb获取截图，避免UTF-8编码错误
                    try:
                        import subprocess
                        # 使用exec-out获取原始字节数据，避免文本编码问题
                        result = subprocess.run(
                            f"adb -s {self.serial} exec-out screencap -p",
                            shell=True,
                        )
                        if result.returncode == 0 and result.stdout:
                            import cv2
                            import numpy as np
                            # 直接从字节数据解码PNG
                            nparr = np.frombuffer(result.stdout, np.uint8)
                            img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
                            if img is not None:
                                # 转换为PIL Image格式
                                from PIL import Image
                                img_rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
                                return Image.fromarray(img_rgb)
                            else:
                                print("⚠️ 警告：无法解码截图数据")
                        else:
                            print("⚠️ 警告：screencap命令返回空数据")
                    except subprocess.TimeoutExpired:
                        print("❌ 截图超时")
                    except Exception as e:
                        print(f"获取截图失败: {e}")
                    return None

                def shell(self, cmd, encoding='utf-8', timeout=None):
                    # 执行shell命令，兼容encoding参数
                    try:
                        import subprocess

                        # 如果encoding为None，使用字节模式
                        if encoding is None:
                            result = subprocess.run(
                                f"adb -s {self.serial} shell {cmd}",
                                shell=True, capture_output=True, timeout=timeout
                            )
                            return result.stdout  # 返回字节数据
                        else:
                            result = subprocess.run(
                                f"adb -s {self.serial} shell {cmd}",
                                shell=True, capture_output=True, text=True, timeout=timeout
                            )
                            return result.stdout  # 返回文本数据
                    except subprocess.TimeoutExpired:
                        print(f"❌ Shell命令超时: {cmd}")
                        return "" if encoding else b""
                    except Exception as e:
                        print(f"执行shell命令失败: {e}")
                        return "" if encoding else b""

            # 创建设备代理
            device_proxy = DeviceProxy(self.device.serial)

            # 创建ActionProcessor实例，传递detect_buttons函数以启用AI检测功能
            action_processor = ActionProcessor(
                device=device_proxy,
                device_name=self.device.serial,
                log_txt_path=log_txt_path,
                detect_buttons_func=self.detect_buttons
            )            # 设置设备账号信息（静默模式，避免重复打印）
            if self.device_account:
                action_processor.set_device_account(self.device_account)
                # 注释掉重复的日志输出，因为账号已在初始分配时打印过
                # 执行操作（使用经过参数替换的step_copy）
            result = action_processor.process_action(
                step_copy, step_idx, log_dir
            )

            # 处理返回值（支持ActionResult对象和旧式三元组）
            if isinstance(result, ActionResult):
                success = result.success
                has_executed = result.executed
                should_continue = result.should_continue
            elif isinstance(result, tuple) and len(result) >= 2:
                # 旧式返回格式 (success, has_executed, should_continue)
                success = result[0] if len(result) > 0 else False
                has_executed = result[1] if len(result) > 1 else False
                should_continue = result[2] if len(result) > 2 else True
            else:
                # 单个布尔值或其他格式
                success = bool(result)
                has_executed = bool(result)
                should_continue = True            # 🔧 修复：只清理临时目录，保留设备报告目录
            if temp_log_dir and os.path.exists(temp_log_dir):
                try:
                    import shutil
                    shutil.rmtree(temp_log_dir, ignore_errors=True)
                    print(f"🗑️ 清理临时目录: {temp_log_dir}")
                except:
                    pass
            else:
                print(f"🔧 保留设备报告目录: {log_dir}")

            return success and has_executed

        except Exception as e:
            print(f"❌ 路由到ActionProcessor失败: {e}")
            import traceback
            traceback.print_exc()
            return False

    def handle_system_dialogs(
        self,
        max_duration: float = 5.0,
        retry_interval: float = 0.5,
        duration: float = 1.0
    ) -> bool:
        """
        检查并自动处理系统弹窗。
        参数:
            max_duration: 最多等待时间（秒）
            retry_interval: 每次检测间隔（秒）
            duration: 点击后等待弹窗消失时间（秒）
        """
        import time
        start = time.time()
        handled = False

        while time.time() - start < max_duration:
            found = self._detect_and_click_dialog()
            if found:
                handled = True
                time.sleep(duration)
                # 找到并处理了弹窗后，继续检测是否还有其他弹窗
                continue
            else:
                # 没有找到弹窗，等待一段时间后继续检测
                time.sleep(retry_interval)

        return handled

    def _detect_and_click_dialog(self) -> bool:
        """检测并点击系统弹窗按钮"""
        try:
            if DeviceScriptReplayer is None:
                print("⚠️ DeviceScriptReplayer不可用，无法检测系统弹窗")
                return False
            input_handler = DeviceScriptReplayer(self.device.serial)
            # 从 ElementPatterns 类获取系统弹窗模式
            try:
                from apps.scripts.enhanced_input_handler import ElementPatterns
                patterns = ElementPatterns.SYSTEM_DIALOG_PATTERNS
            except (ImportError, AttributeError) as e:
                try:
                    from enhanced_input_handler import ElementPatterns
                    patterns = ElementPatterns.SYSTEM_DIALOG_PATTERNS
                except (ImportError, AttributeError):
                    print(f"⚠️ 无法导入或访问SYSTEM_DIALOG_PATTERNS: {e}")
                    return False

            if not patterns:
                print("⚠️ SYSTEM_DIALOG_PATTERNS 为空")
                return False

            xml_content = input_handler.get_ui_hierarchy()
            if not xml_content:
                return False

            # 尝试使用 input_handler 的 _parse_ui_xml 方法，如果没有则实现一个简单的解析
            if hasattr(input_handler, "_parse_ui_xml"):
                elements = input_handler._parse_ui_xml(xml_content)
            else:
                # 简单占位解析
                import xml.etree.ElementTree as ET
                elements = []
                try:
                    root = ET.fromstring(xml_content)
                    for elem in root.iter():
                        elements.append(elem.attrib)
                except Exception as e:
                    print(f"XML解析失败: {e}")
                    return False

            # 首先查找优先级按钮
            priority_buttons = []
            other_buttons = []

            for element in elements:
                if not element.get('clickable', False):
                    continue

                text = element.get('text', '')
                text_lower = text.lower()

                # 检查是否匹配优先级关键词
                is_priority = False
                for kw in patterns.get('priority_keywords', []):
                    if kw.lower() in text_lower:
                        priority_buttons.append((element, text))
                        is_priority = True
                        break

                # 如果不是优先级按钮，检查是否匹配一般关键词
                if not is_priority:
                    for kw in patterns['text_hints']:
                        if kw.lower() in text_lower:
                            other_buttons.append((element, text))
                            break

            # 优先点击优先级按钮
            if priority_buttons:
                element, text = priority_buttons[0]
                print(f"⚡ 检测到优先级系统弹窗按钮: '{text}'，自动点击")
                input_handler.tap_element(element)
                return True
            elif other_buttons:
                element, text = other_buttons[0]
                print(f"⚡ 检测到系统弹窗按钮: '{text}'，自动点击")
                input_handler.tap_element(element)
                return True

            return False
        except Exception as e:
            print(f"❌ 检测系统弹窗时出错: {e}")
            return False

    def process_script(self, script_path: str) -> ActionResult:
        """
        回放单个脚本 - 支持参数化和传统格式

        Args:
            script_path: 脚本文件路径

        Returns:
            回放是否成功
        """
        print(f"📜 开始回放脚本: {script_path}")

        try:
            # 读取脚本文件
            with open(script_path, 'r', encoding='utf-8') as f:
                script_content = f.read()            # 解析JSON脚本
            import json
            script_json = json.loads(script_content)

            if isinstance(script_json, list):
                # 数组格式
                print("📋 检测到数组格式脚本")
                steps = script_json
                defaults = {}
                meta = {}
                
                # 检查第一个步骤是否是global定义
                if len(steps) > 0:
                    first_step = steps[0]
                    # 支持多种标记方式
                    is_global_step = False
                    # 默认排除的字段。不作为默认参数传递给后续步骤
                    exclude_keys = []
                    
                    if first_step.get('type') == 'global':
                        is_global_step = True
                        exclude_keys = ['type', 'remark']
                    
                    if is_global_step:
                        # 第一个步骤是 global 定义,提取所有参数(排除标记字段)
                        defaults = {k: v for k, v in first_step.items() 
                                   if k not in exclude_keys}
                        steps = steps[1:]  # 移除defaults步骤
                        print(f"   应用全局默认值: {defaults}")
                        print(f"   排除的字段: {exclude_keys}")
            else:
                # 对象格式
                print("📋 检测到对象格式脚本")
                steps = script_json.get('steps', [])
                defaults = script_json.get('defaults', {})
                meta = script_json.get('meta', {})
                if defaults:
                    print(f"   应用全局默认值: {defaults}")
            
            # 执行每个步骤
            for step_idx, step in enumerate(steps):
                # 合并全局默认值到步骤(步骤中的值优先级更高)
                if defaults:
                    print(f"\n🔧 步骤 {step_idx + 1} 合并前: {step}")
                    for key, value in defaults.items():
                        if key not in step:
                            step[key] = value
                            print(f"   ✅ 应用defaults: {key}={value}")
                        else:
                            print(f"   ⏭️  跳过(已存在): {key}={step[key]}")
                    print(f"🔧 步骤 {step_idx + 1} 合并后: {step}")
                # 兼容两种脚本格式：新格式使用action字段，旧格式使用class字段
                action = step.get('action')
                target_selector = step.get('target_selector', {})
                text = step.get('text', '')
                remark = step.get('remark', '')

                print(f"🔧 执行步骤 {step_idx + 1}: action={action}, remark={remark}")
                try:
                    if action == 'delay':
                        # 延迟操作
                        delay_time = step.get('seconds', 1.0)
                        print(f"⏰ 延迟 {delay_time} 秒")
                        time.sleep(float(delay_time))
                    elif action == 'input':
                        # 输入操作 - 支持参数化
                        # 注意：参数替换已在input_text_with_focus_detection方法中处理
                        print(f"⌨️ 执行输入操作: {text[:30]}{'...' if len(text) > 30 else ''}")

                        if DeviceScriptReplayer is None:
                            print("❌ DeviceScriptReplayer不可用，无法执行输入")
                            continue
                        input_handler = DeviceScriptReplayer(self.device.serial)
                        if target_selector.get('type'):
                            # 参数化方式
                            success = input_handler.input_text_with_focus_detection(text, target_selector)
                        else:
                            # 传统方式
                            ui_xml = input_handler.get_ui_hierarchy()
                            if not ui_xml:
                                print(f"❌ 获取UI结构失败，无法执行输入")
                                continue

                            elements = input_handler._parse_ui_xml(ui_xml)
                            input_field = input_handler.find_best_input_field(target_selector)
                            if input_field:
                                success = input_handler.input_text_with_focus_detection(text, target_selector)
                            else:
                                print("❌ 未找到输入框")
                                success = False

                        if not success:
                            print(f"❌ 输入操作失败")
                            continue

                    elif action == 'checkbox':
                        # checkbox操作 - 支持参数化
                        print(f"☑️ 执行checkbox勾选操作，已废弃。改为使用yolo_class识别")
                        if DeviceScriptReplayer is None:
                            print("❌ DeviceScriptReplayer不可用，无法执行checkbox操作")
                            continue
                        input_handler = DeviceScriptReplayer(self.device.serial)
                        success = input_handler.perform_checkbox_action(target_selector)

                        if not success:
                            print(f"❌ checkbox操作失败")
                            continue

                    elif action == 'click':
                        # 点击操作 - 路由到ActionProcessor以获得更好的参数处理
                        print(f"👆 执行点击操作")
                        success = self._route_to_action_processor(step, step_idx, 'click')
                        if not success:
                            print(f"❌ click 操作失败")
                            continue

                    elif action == 'app_start':
                        # 新增支持: 应用启动操作
                        print(f"🚀 执行应用启动操作")
                        success = self._route_to_action_processor(step, step_idx, 'app_start')
                        if not success:
                            print(f"❌ app_start 操作失败")
                            continue

                    # 新增支持: AI检测点击操作 (Priority模式)
                    elif action == 'ai_detection_click':
                        print(f"🎯 执行AI检测点击操作:")
                        success = self._route_to_action_processor(step, step_idx, 'ai_detection_click')
                        if not success:
                            print(f"❌ ai_detection_click 操作失败")
                            continue
                    
                    # 新增支持: OCR检测点击操作 (ai_detection_click的别名)
                    elif action == 'ocr_click':
                        print(f"🔍 执行OCR检测点击操作:")
                        success = self._route_to_action_processor(step, step_idx, 'ai_detection_click')
                        if not success:
                            print(f"❌ ocr_click 操作失败")
                            continue

                    # 新增支持: 滑动操作 (Priority模式)
                    elif action == 'swipe':
                        print(f"👆 执行滑动操作")
                        success = self._route_to_action_processor(step, step_idx, 'swipe')
                        if not success:
                            print(f"❌ swipe 操作失败")
                            continue
                     # 新增支持: 备用点击操作 (Priority模式)
                    elif action == 'fallback_click':
                        print(f"🔄 执行备用点击操作")
                        success = self._route_to_action_processor(step, step_idx, 'fallback_click')
                        if not success:
                            print(f"❌ fallback_click 操作失败")
                            continue

                    # 新增支持: 三个关键等待和重试操作
                    elif action == 'wait_for_appearance':
                        print(f"⏳ 执行等待元素出现操作")
                        success = self._route_to_action_processor(step, step_idx, 'wait_for_appearance')
                        if not success:
                            print(f"❌ wait_for_appearance 操作失败")
                            continue
                    elif action == 'wait_for_disappearance':
                        print(f"⏳ 执行等待元素消失操作")
                        success = self._route_to_action_processor(step, step_idx, 'wait_for_disappearance')
                        if not success:
                            print(f"❌ wait_for_disappearance 操作失败")
                            continue

                    elif action == 'wait_if_exists':
                        print(f"⏳ 执行等待元素存在操作")
                        success = self._route_to_action_processor(step, step_idx, 'wait_if_exists')
                        if not success:
                            print(f"❌ wait_if_exists 操作失败")
                            continue

                    elif action == 'wait_for_stable':
                        print(f"⏳ 执行等待界面稳定操作")
                        success = self._route_to_action_processor(step, step_idx, 'wait_for_stable')
                        if not success:
                            print(f"❌ wait_for_stable 操作失败")
                            continue

                    elif action == 'retry_until_success':
                        print(f"🔄 执行重试直到成功操作")
                        success = self._route_to_action_processor(step, step_idx, 'retry_until_success')
                        if not success:
                            print(f"❌ retry_until_success 操作失败")
                            continue

                    # 新增支持: 设备预处理操作
                    elif action == 'device_preparation':
                        print(f"🔧 执行设备预处理操作")
                        success = self._route_to_action_processor(step, step_idx, 'device_preparation')
                        if not success:
                            print(f"❌ device_preparation 操作失败")
                            continue

                    else:
                        print(f"⚠️ process_script不支持的操作: {action}，跳过")
                        continue

                    # 步骤执行后延迟（支持从全局defaults继承）
                    sleep_time = step.get('sleep', 0.5)  # 默认0.5秒
                    if sleep_time > 0:
                        print(f"⏳ 步骤执行后等待 {sleep_time} 秒...")
                        time.sleep(sleep_time)
                except Exception as e:
                    print(f"❌ 步骤 {step_idx + 1} 执行异常: {e}")
                    import traceback
                    traceback.print_exc()
                    continue

            print("✅ 脚本回放完成")
            return ActionResult(success=True, message="脚本回放完成")

        except Exception as e:
            print(f"❌ 脚本回放过程中发生错误: {e}")
            import traceback
            traceback.print_exc()
            return ActionResult(success=False, message=f"脚本回放错误: {e}")

    def _handle_click_with_execute_action(self, step, step_idx, log_dir):
        """处理点击后执行其他操作的组合步骤"""
        step_remark = step.get("remark", "")
        execute_action = step.get("execute_action")

        print(f"🎯 执行组合操作: 点击 + {execute_action} - {step_remark}")

        try:
            # 第一步：执行点击操作
            print(f"📍 第1步: 执行AI检测点击")
            click_result = self._handle_ai_detection_click(step, step_idx, log_dir)

            # 检查点击是否成功
            if not click_result.success:
                print(f"❌ 点击操作失败，跳过后续{execute_action}操作")
                return click_result

            print(f"✅ 点击操作成功，等待界面响应...")
            time.sleep(1.0)  # 等待界面响应

            # 第二步：根据execute_action执行相应操作
            if execute_action == "input":
                print(f"📝 第2步: 执行文本输入操作")
                input_result = self._handle_input_after_click(step, step_idx, log_dir)

                if input_result.success:
                    print(f"✅ 组合操作成功: 点击 + 输入")
                    return ActionResult(
                        success=True,
                        message=f"点击并输入操作成功: {step_remark}",
                        details={
                            "operation": "click_with_input",
                            "click_result": click_result.details,
                            "input_result": input_result.details
                        }
                    )
                else:
                    print(f"❌ 输入操作失败")
                    return input_result
            else:
                print(f"⚠️ 不支持的execute_action: {execute_action}")
                return ActionResult(
                    success=False,
                    message=f"不支持的execute_action: {execute_action}",
                    details={"operation": "click_with_execute", "error": "unsupported_execute_action"}
                )

        except Exception as e:
            print(f"❌ 组合操作过程中发生异常: {e}")
            traceback.print_exc()
            return ActionResult(
                success=False,
                message=f"组合操作异常: {str(e)}",
                details={"operation": "click_with_execute", "error": str(e)}
            )

    def _handle_input_after_click(self, step, step_idx, log_dir):
        """处理点击后的输入操作（基于原_handle_input逻辑）"""
        input_text = step.get("text", "")
        step_remark = step.get("remark", "")

        # 智能账号分配：如果需要账号参数但没有分配，尝试自动分配
        if ("${account:username}" in input_text or "${account:password}" in input_text):
            if not self.device_account:
                print("🔄 检测到需要账号参数但设备未分配账号，尝试自动分配...")
                self._auto_allocate_device_account()

        # 参数替换处理：${account:username} 和 ${account:password}
        if "${account:username}" in input_text:
            if self.device_account and len(self.device_account) >= 1:
                input_text = input_text.replace("${account:username}", self.device_account[0])
                print(f"✅ 替换用户名参数: {self.device_account[0]}")
            else:
                device_serial = getattr(self.device, 'serial', self.device_name)
                print(f"❌ 错误: 设备 {device_serial} 没有分配账号，无法替换用户名参数")
                return ActionResult(
                    success=False,
                    message="设备没有分配账号，无法替换用户名参数",
                    details={"operation": "input_after_click", "error": "no_account_assigned"}
                )

        if "${account:password}" in input_text:
            if self.device_account and len(self.device_account) >= 2:
                input_text = input_text.replace("${account:password}", self.device_account[1])
                print(f"✅ 替换密码参数")
            else:
                device_serial = getattr(self.device, 'serial', self.device_name)
                print(f"❌ 错误: 设备 {device_serial} 没有分配账号，无法替换密码参数")
                return ActionResult(
                    success=False,
                    message="设备没有分配账号，无法替换密码参数",
                    details={"operation": "input_after_click", "error": "no_account_assigned"}
                )

        print(f"📝 执行文本输入: {input_text}")

        try:
            # 直接使用adb input text命令输入文本
            escaped_text = input_text.replace(" ", "%s").replace("'", "\\'").replace('"', '\\"')
            self.device.shell(f"input text '{escaped_text}'")

            print(f"✅ 文本输入成功: {input_text}")

            # 创建screen对象以支持报告截图显示
            screen_data = self._create_unified_screen_object(
                log_dir,
                pos_list=[],
                confidence=1.0,
                rect_info=[]
            )

            # 记录输入操作日志
            timestamp = time.time()
            input_entry = {
                "tag": "function",
                "depth": 1,
                "time": timestamp,
                "data": {
                    "name": "input_text",
                    "call_args": {"text": input_text},
                    "start_time": timestamp,
                    "ret": input_text,
                    "end_time": timestamp + 0.5,
                    "desc": step_remark or f"输入文本: {input_text}",
                    "title": f"#{step_idx+1} {step_remark or f'输入文本: {input_text}'}"
                }
            }            # 添加screen对象到日志条目（如果可用）
            if screen_data:
                input_entry["data"]["screen"] = screen_data

            # 添加 executed 字段到日志条目
            input_entry["data"]["executed"] = True

            self._write_log_entry(input_entry)

            return ActionResult(
                success=True,
                message=f"文本输入成功: {input_text}",
                details={
                    "operation": "input_after_click",
                    "text": input_text,
                    "has_screenshot": screen_data is not None
                }
            )

        except Exception as e:
            print(f"❌ 文本输入过程中发生异常: {e}")
            traceback.print_exc()
            return ActionResult(
                success=False,
                message=f"文本输入异常: {str(e)}",
                details={"operation": "input_after_click", "error": str(e)}
            )

    # =================== Priority 模式专用处理方法 ===================

    def _handle_ai_detection_click_priority_mode(self, step, cycle_count, log_dir):
        """Priority模式专用的AI检测点击处理 - 只在成功时记录日志和截图"""
        step_class = step.get("yolo_class")
        step_remark = step.get("remark", "")

        if not step_class or step_class == "unknown":
            return ActionResult(
                success=False,
                message="AI检测点击步骤缺少有效的检测类别",
                details={"operation": "ai_detection_click_priority", "error": "invalid_class"},
                executed=False
            )

        try:
            # 获取屏幕截图
            screenshot = get_device_screenshot(self.device)
            if screenshot is None:
                return ActionResult(
                    success=False,
                    message="无法获取设备屏幕截图",
                    details={"operation": "ai_detection_click_priority", "error": "screenshot_failed"},
                    executed=False
                )

            import cv2
            import numpy as np
            frame = cv2.cvtColor(np.array(screenshot), cv2.COLOR_RGB2BGR)

            # 使用AI检测（如果可用）
            if self.detect_buttons:
                step_confidence = step.get("confidence", 0.6)
                
                # 提取OCR相关参数
                use_ocr = step.get("use_ocr", False)
                ocr_keywords = step.get("ocr_keywords", None)
                ocr_min_score = step.get("ocr_min_score", 0.5)
                
                if use_ocr:
                    print(f"🔍 [Priority模式] OCR已启用 - 关键字: {ocr_keywords}, 最小置信度: {ocr_min_score}")
                
                success, detection_result = self.detect_buttons(
                    frame, 
                    target_class=step_class, 
                    conf_threshold=step_confidence,
                    use_ocr=use_ocr,
                    ocr_keywords=ocr_keywords,
                    ocr_min_score=ocr_min_score
                )

                if success and detection_result[0] is not None:
                    # 解包检测结果,包含可选的OCR结果
                    if len(detection_result) >= 4:
                        x, y, detected_class, ocr_result = detection_result
                    else:
                        # 兼容旧版本返回格式
                        x, y, detected_class = detection_result[:3]
                        ocr_result = None

                    # 执行点击操作
                    self.device.shell(f"input tap {int(x)} {int(y)}")

                    # 只在成功时生成截图和记录日志
                    screen_data = self._create_unified_screen_object(
                        log_dir,
                        pos_list=[[int(x), int(y)]],
                        confidence=step_confidence,
                        rect_info=[{"left":int(x)-20,"top":int(y)-20,"width":40,"height":40}]
                    )

                    timestamp = time.time()
                    
                    # 构建日志条目,包含OCR信息
                    call_args = {
                        "target_class": step_class, 
                        "position": [int(x), int(y)]
                    }
                    if use_ocr and ocr_result:
                        call_args["ocr_texts"] = ocr_result.get("texts", [])
                        call_args["ocr_scores"] = ocr_result.get("scores", [])
                        call_args["ocr_matched"] = ocr_result.get("has_match", False)
                    
                    ai_entry = {
                        "tag": "function",
                        "depth": 1,
                        "time": timestamp,
                        "data": {
                            "name": "ai_detection_click",
                            "call_args": call_args,
                            "start_time": timestamp,
                            "ret": [int(x), int(y)],
                            "end_time": timestamp,
                            "desc": step_remark or f"[循环{cycle_count}] AI检测点击({step_class})",
                            "title": f"#{cycle_count} {step_remark or f'AI检测点击({step_class})'}",
                            "executed": True
                        }
                    }
                    if screen_data:
                        ai_entry["data"]["screen"] = screen_data

                    self._write_log_entry(ai_entry)

                    return ActionResult(
                        success=True,
                        message=f"AI检测点击成功: {step_class}",
                        details={"operation": "ai_detection_click_priority", "target_class": step_class, "position": [int(x), int(y)]},
                        executed=True
                    )
                else:
                    # 检测失败，不记录日志，只返回失败结果
                    return ActionResult(
                        success=False,
                        message=f"AI检测未命中: {step_class}",
                        details={"operation": "ai_detection_click_priority", "target_class": step_class},
                        executed=False
                    )
            else:
                return ActionResult(
                    success=False,
                    message="AI检测功能不可用",
                    details={"operation": "ai_detection_click_priority", "error": "ai_detection_unavailable"},
                    executed=False
                )

        except Exception as e:
            print(f"❌ Priority模式AI检测点击过程中发生异常: {e}")
            import traceback
            traceback.print_exc()
            return ActionResult(
                success=False,
                message=f"AI检测点击异常: {str(e)}",
                details={"operation": "ai_detection_click_priority", "error": str(e)},
                executed=False
            )

    def _handle_swipe_priority_mode(self, step, cycle_count, log_dir):
        """Priority模式专用的滑动处理 - 总是记录日志和截图"""
        start_x = step.get("start_x")
        start_y = step.get("start_y")
        end_x = step.get("end_x")
        end_y = step.get("end_y")
        duration = step.get("duration", 300)
        step_remark = step.get("remark", "")

        if start_x is None or start_y is None or end_x is None or end_y is None:
            return ActionResult(
                success=False,
                message="swipe 步骤缺少必要的坐标参数",
                details={"operation": "swipe_priority", "error": "missing_coordinates"},
                executed=False
            )

        try:
            # 执行ADB滑动命令
            self.device.shell(f"input swipe {int(start_x)} {int(start_y)} {int(end_x)} {int(end_y)} {int(duration)}")

            # 滑动操作总是成功，生成截图和记录日志
            screen_data = self._create_unified_screen_object(
                log_dir,
                pos_list=[[int(start_x), int(start_y)], [int(end_x), int(end_y)]],
                confidence=1.0,
                rect_info=[{
                    "left": min(int(start_x), int(end_x)) - 20,
                    "top": min(int(start_y), int(end_y)) - 20,
                    "width": abs(int(end_x) - int(start_x)) + 40,
                    "height": abs(int(end_y) - int(start_y)) + 40
                }]
            )

            timestamp = time.time()
            swipe_entry = {"oss_pic_pth": "",

                "tag": "function",
                "depth": 1,
                "time": timestamp,
                "data": {
                    "name": "swipe",
                    "call_args": {
                        "start": [int(start_x), int(start_y)],
                        "end": [int(end_x), int(end_y)],
                        "duration": int(duration)
                    },
                    "start_time": timestamp,
                    "ret": {
                        "start_pos": [int(start_x), int(start_y)],
                        "end_pos": [int(end_x), int(end_y)]
                    },
                    "end_time": timestamp + (duration / 1000.0),
                    "desc": step_remark or f"[循环{cycle_count}] 滑动操作",
                    "title": f"#{cycle_count} {step_remark or '滑动操作'}",
                    "executed": True
                }
            }

            if screen_data:
                swipe_entry["data"]["screen"] = screen_data

            self._write_log_entry(swipe_entry)

            # 滑动后等待一段时间让UI响应
            time.sleep(duration / 1000.0 + 0.5)

            return ActionResult(
                success=True,
                message=f"滑动操作完成: ({start_x}, {start_y}) -> ({end_x}, {end_y})",
                details={
                    "operation": "swipe",
                    "start_position": (start_x, start_y),
                    "end_position": (end_x, end_y),
                    "duration": duration,
                    "has_screenshot": screen_data is not None
                },
                executed=True
            )

        except Exception as e:
            print(f"❌ Priority模式滑动过程中发生异常: {e}")
            import traceback
            traceback.print_exc()
            return ActionResult(
                success=False,
                message=f"滑动操作异常: {str(e)}",
                details={"operation": "swipe_priority", "error": str(e)},
                executed=False
            )

    def _handle_fallback_click_priority_mode(self, step, cycle_count, log_dir):
        """Priority模式专用的备选点击处理 - 总是记录日志和截图"""
        step_remark = step.get("remark", "")

        if "relative_x" not in step or "relative_y" not in step:
            return ActionResult(
                success=False,
                message="fallback_click 步骤缺少相对坐标信息",
                details={"operation": "fallback_click_priority", "error": "missing_relative_coordinates"},
                executed=False
            )

        try:
            # 获取屏幕截图以获取分辨率
            screenshot = get_device_screenshot(self.device)
            if screenshot is None:
                return ActionResult(
                    success=False,
                    message="无法获取屏幕截图",
                    details={"operation": "fallback_click_priority", "error": "screenshot_failed"},
                    executed=False
                )

            import cv2
            import numpy as np
            frame = cv2.cvtColor(np.array(screenshot), cv2.COLOR_RGB2BGR)
            height, width = frame.shape[:2]

            # 计算绝对坐标
            rel_x = float(step["relative_x"])
            rel_y = float(step["relative_y"])
            abs_x = int(width * rel_x)
            abs_y = int(height * rel_y)

            # 执行点击操作
            self.device.shell(f"input tap {abs_x} {abs_y}")

            # 备选点击总是成功，生成截图和记录日志
            screen_data = self._create_unified_screen_object(
                log_dir,
                pos_list=[[abs_x, abs_y]],
                confidence=1.0,
                rect_info=[{
                    "left": max(0, abs_x - 50),
                    "top": max(0, abs_y - 50),
                    "width": 100,
                    "height": 100
                }]
            )

            timestamp = time.time()
            click_entry = {
                "tag": "function",
                "depth": 1,
                "time": timestamp,
                "data": {
                    "name": "touch",
                    "call_args": {"v": [abs_x, abs_y]},
                    "start_time": timestamp,
                    "ret": [abs_x, abs_y],
                    "end_time": timestamp + 0.1,
                    "desc": step_remark or f"[循环{cycle_count}] 备选点击({rel_x:.3f}, {rel_y:.3f})",
                    "title": f"#{cycle_count} {step_remark or f'备选点击({rel_x:.3f}, {rel_y:.3f})'}",
                    "executed": True
                }
            }

            if screen_data:
                click_entry["data"]["screen"] = screen_data

            self._write_log_entry(click_entry)

            return ActionResult(
                success=True,
                message=f"备选点击成功: ({rel_x:.3f}, {rel_y:.3f}) -> ({abs_x}, {abs_y})",
                details={
                    "operation": "fallback_click_priority",
                    "relative_position": {"x": rel_x, "y": rel_y},
                    "absolute_position": {"x": abs_x, "y": abs_y},
                    "screen_size": {"width": width, "height": height}
                },
                executed=True
            )

        except Exception as e:
            print(f"❌ Priority模式备选点击过程中发生异常: {e}")
            import traceback
            traceback.print_exc()
            return ActionResult(
                success=False,
                message=f"备选点击失败: {str(e)}",
                details={"operation": "fallback_click_priority", "error": str(e)},
                executed=False
            )

    def _ocr_match_first_strategy(self, frame, ocr_keywords, ocr_min_score):
        """第一个匹配策略：遇到第一个符合条件的就返回"""
        print(f"🎯 使用第一个匹配策略")
        
        try:
            from apps.scripts.replay_script import _get_ocr_pipeline
            from apps.ocr.services.keyword_filter import KeywordFilter
            
            # 获取OCR Pipeline
            pipeline = _get_ocr_pipeline()
            if pipeline is None:
                print(f"❌ OCR Pipeline未初始化")
                return False, None
            
            # 对整个屏幕进行OCR识别
            predictions = list(pipeline.predict(
                [frame],
                use_doc_orientation_classify=False,
                use_doc_unwarping=False,
                use_textline_orientation=False,
            ))
            
            if not predictions:
                return False, None
            
            # 提取识别结果
            result = predictions[0]
            res_json = getattr(result, "json", {}).get("res", {})
            
            texts = res_json.get("rec_texts", [])
            scores = res_json.get("rec_scores", [])
            rec_polys = res_json.get("rec_polys", [])
            
            if not texts:
                return False, None
            
            # 打印所有识别到的OCR文本
            print(f"📋 页面OCR识别结果 (共 {len(texts)} 个文本):")
            print("=" * 80)
            for i, text in enumerate(texts):
                if not text or not text.strip():
                    continue
                score = float(scores[i]) if i < len(scores) else 0.0
                polygon = rec_polys[i] if i < len(rec_polys) else []
                if polygon:
                    x_coords = [p[0] for p in polygon]
                    y_coords = [p[1] for p in polygon]
                    center_x = int(sum(x_coords) / len(x_coords))
                    center_y = int(sum(y_coords) / len(y_coords))
                    print(f"[{i+1}] '{text.strip()}' | 置信度: {score:.2f} | 位置: ({center_x}, {center_y})")
            print("=" * 80)
            
            # 设置关键字过滤器
            keyword_filter_config = {
                "enabled": True,
                "keywords": ocr_keywords,
                "fuzzy_match": True,
                "fuzzy_similarity": 0.80,
                "ignore_case": True,
                "ignore_spaces": True,
                "ignore_digits": False,
                "min_confidence": ocr_min_score
            }
            
            keyword_filter = KeywordFilter(keyword_filter_config)
            
            # 查找第一个匹配项
            for i, text in enumerate(texts):
                if not text or not text.strip():
                    continue
                
                score = float(scores[i]) if i < len(scores) else 0.0
                if score < ocr_min_score:
                    continue
                
                # 构造OCR结果格式用于过滤
                ocr_results = [{
                    "image_path": "",
                    "texts": [text.strip()],
                    "scores": [score],
                    "has_match": True
                }]
                
                filtered_results = keyword_filter.filter_results(ocr_results)
                
                if len(filtered_results) > 0:
                    # 找到第一个匹配，立即返回
                    polygon = rec_polys[i] if i < len(rec_polys) else []
                    if polygon:
                        x_coords = [p[0] for p in polygon]
                        y_coords = [p[1] for p in polygon]
                        x = int(sum(x_coords) / len(x_coords))
                        y = int(sum(y_coords) / len(y_coords))
                        
                        print(f"✅ OCR第一个匹配[{i}]: '{text.strip()}' 位置: ({x}, {y})")
                        
                        return True, {
                            "position": (x, y),
                            "text": text.strip(),
                            "score": score,
                            "polygon": polygon
                        }
            
            print(f"❌ 未找到匹配关键字 '{ocr_keywords}' 的文本")
            return False, None
            
        except Exception as e:
            print(f"❌ first策略OCR检测异常: {e}")
            return False, None

    def _ocr_match_desc_strategy(self, frame, ocr_keywords, description, ocr_min_score):
        """基于描述的智能匹配策略：根据空间位置描述选择最佳匹配"""
        print(f"🎯 使用描述匹配策略: {description}")
        
        try:
            from apps.scripts.replay_script import _get_ocr_pipeline
            from apps.ocr.services.keyword_filter import KeywordFilter
            
            # 获取OCR Pipeline
            pipeline = _get_ocr_pipeline()
            if pipeline is None:
                print(f"❌ OCR Pipeline未初始化")
                return False, None
            
            # 对整个屏幕进行OCR识别
            predictions = list(pipeline.predict(
                [frame],
                use_doc_orientation_classify=False,
                use_doc_unwarping=False,
                use_textline_orientation=False,
            ))
            
            if not predictions:
                return False, None
            
            # 提取识别结果
            result = predictions[0]
            res_json = getattr(result, "json", {}).get("res", {})
            
            texts = res_json.get("rec_texts", [])
            scores = res_json.get("rec_scores", [])
            rec_polys = res_json.get("rec_polys", [])
            
            if not texts:
                return False, None
            
            # 打印所有识别到的OCR文本
            print(f"📋 页面OCR识别结果 (共 {len(texts)} 个文本):")
            print("=" * 80)
            for i, text in enumerate(texts):
                if not text or not text.strip():
                    continue
                score = float(scores[i]) if i < len(scores) else 0.0
                polygon = rec_polys[i] if i < len(rec_polys) else []
                if polygon:
                    x_coords = [p[0] for p in polygon]
                    y_coords = [p[1] for p in polygon]
                    center_x = int(sum(x_coords) / len(x_coords))
                    center_y = int(sum(y_coords) / len(y_coords))
                    print(f"[{i+1}] '{text.strip()}' | 置信度: {score:.2f} | 位置: ({center_x}, {center_y})")
            print("=" * 80)
            
            # 设置关键字过滤器
            keyword_filter_config = {
                "enabled": True,
                "keywords": ocr_keywords,
                "fuzzy_match": True,
                "fuzzy_similarity": 0.80,
                "ignore_case": True,
                "ignore_spaces": True,
                "ignore_digits": False,
                "min_confidence": ocr_min_score
            }
            
            keyword_filter = KeywordFilter(keyword_filter_config)
            
            # 使用统一的关键字预处理方法
            keywords_list, normalized_keywords = self._prepare_keywords(ocr_keywords)
            
            # 收集所有匹配的关键字文本
            target_matches = []
            all_texts_with_positions = []
            
            for i, text in enumerate(texts):
                if not text or not text.strip():
                    continue
                
                score = float(scores[i]) if i < len(scores) else 0.0
                if score < ocr_min_score:
                    continue
                
                polygon = rec_polys[i] if i < len(rec_polys) else []
                if polygon:
                    x_coords = [p[0] for p in polygon]
                    y_coords = [p[1] for p in polygon]
                    center_x = int(sum(x_coords) / len(x_coords))
                    center_y = int(sum(y_coords) / len(y_coords))
                    
                    # 记录所有文本位置（用于查找参考元素）
                    all_texts_with_positions.append({
                        "text": text.strip(),
                        "position": (center_x, center_y),
                        "polygon": polygon
                    })
                    
                    # 检查是否匹配目标关键字
                    ocr_results = [{
                        "image_path": "",
                        "texts": [text.strip()],
                        "scores": [score],
                        "has_match": True
                    }]
                    
                    filtered_results = keyword_filter.filter_results(ocr_results)
                    
                    if len(filtered_results) > 0:
                        target_matches.append({
                            "index": i,
                            "text": text.strip(),
                            "score": score,
                            "polygon": polygon,
                            "position": (center_x, center_y)
                        })
            
            # 若存在严格匹配（文本与关键字完全一致，忽略大小写与空格），优先保留
            used_strict_keyword = False
            if target_matches:
                strict_candidates = []
                for m in target_matches:
                    norm_text = self._normalize_text(m["text"])
                    if norm_text in normalized_keywords:
                        strict_candidates.append(m)
                if strict_candidates:
                    print(f"✅ 使用严格关键字匹配，保留 {len(strict_candidates)} 个候选")
                    target_matches = strict_candidates
                    used_strict_keyword = True
                else:
                    print("⚠️ 未找到严格关键字匹配，回退到包含/模糊匹配结果")
            
            if not target_matches:
                print(f"❌ 未找到匹配关键字 '{ocr_keywords}' 的文本")
                return False, None
            
            # 根据描述选择最佳匹配
            best_match = self._select_match_by_description(target_matches, all_texts_with_positions, description)
            
            if best_match:
                # 若未使用严格匹配，但候选文本包含关键字，则尝试对子串进行近似定位
                adjust_pos = best_match["position"]
                if not used_strict_keyword and keywords_list:
                    raw_text = best_match["text"] or ""
                    # 取第一个出现的关键字进行近似定位
                    chosen_kw = None
                    chosen_idx = -1
                    for kw in keywords_list:
                        if not kw:
                            continue
                        idx = raw_text.find(kw)
                        if idx >= 0:
                            chosen_kw = kw
                            chosen_idx = idx
                            break
                    if chosen_kw is not None and best_match.get("polygon"):
                        poly = best_match["polygon"] or []
                        try:
                            xs = [p[0] for p in poly]
                            ys = [p[1] for p in poly]
                            left, right = min(xs), max(xs)
                            top, bottom = min(ys), max(ys)
                            text_len = max(len(raw_text), 1)
                            center_ratio = (chosen_idx + len(chosen_kw) / 2.0) / text_len
                            # 近似计算子串中心点（假设水平排版）
                            sub_x = int(left + (right - left) * center_ratio)
                            sub_y = int((top + bottom) / 2)
                            adjust_pos = (sub_x, sub_y)
                            print(f"🔧 子串定位: 在 '{raw_text}' 中定位 '{chosen_kw}' -> {adjust_pos}")
                        except Exception as _:
                            pass
                print(f"✅ OCR描述匹配[{best_match['index']}]: '{best_match['text']}' 位置: {adjust_pos}")
                return True, {
                    "position": adjust_pos,
                    "text": best_match["text"],
                    "score": best_match["score"],
                    "polygon": best_match["polygon"]
                }
            else:
                # 使用desc策略时，找不到参考文本说明页面状态不对（未完全加载/账号未登录）
                # 返回False触发重试，等待页面完全加载
                print(f"❌ 根据描述 '{description}' 未找到合适的匹配（可能页面未完全加载）")
                print(f"⚠️ 建议：等待页面完全加载后再重试")
                return False, None
                
        except Exception as e:
            print(f"❌ desc策略OCR检测异常: {e}")
            return False, None

    def _select_match_by_description(self, matches, all_texts_with_positions, description):
        """
        根据描述选择最佳匹配
        
        严格逻辑：
        1. 如果有方位描述，必须找到参考文本并验证方位关系
        2. 找不到参考文本或不满足方位关系，返回None（视为目标未出现）
        3. 只有在完全没有描述时，才允许返回单个匹配
        """
        if not matches:
            return None
        
        # 解析空间位置描述
        reference_text = None
        relation = None
        
        # 解析类似 "shya27 的上方" 的描述
        if " 的上方" in description or " 上方" in description:
            relation = "above"
            reference_text = description.replace(" 的上方", "").replace(" 上方", "").strip()
        elif " 的下方" in description or " 下方" in description:
            relation = "below"
            reference_text = description.replace(" 的下方", "").replace(" 下方", "").strip()
        elif " 的左边" in description or " 左边" in description:
            relation = "left"
            reference_text = description.replace(" 的左边", "").replace(" 左边", "").strip()
        elif " 的右边" in description or " 右边" in description:
            relation = "right"
            reference_text = description.replace(" 的右边", "").replace(" 右边", "").strip()
        
        # 如果有方位描述，必须严格验证
        if reference_text and relation:
            print(f"🔍 解析描述: 寻找相对于 '{reference_text}' {relation} 的元素")
            
            # 查找参考元素的位置
            reference_position = self._find_reference_position(reference_text, all_texts_with_positions)
            
            if reference_position:
                print(f"🔍 参考元素位置: {reference_position}")
                
                # 根据空间关系选择最佳匹配
                best_match = self._select_by_spatial_relation(matches, reference_position, relation)
                
                if best_match:
                    return best_match
                else:
                    # 找到参考文本，但没有符合方位关系的目标
                    print(f"❌ 找到参考文本 '{reference_text}'，但没有符合'{relation}'方位关系的目标")
                    return None
            else:
                # 找不到参考文本，说明页面状态不正确（可能未完全加载）
                # 必须返回None触发重试，避免误点击
                print(f"❌ 未找到参考文本 '{reference_text}'，视为目标未出现")
                print(f"💡 原因：页面可能未完全加载、账号信息未显示或参考文本不存在")
                return None
        
        # 只有在完全没有方位描述时，才允许使用默认匹配
        # 这是为了兼容没有描述的旧配置
        if len(matches) == 1:
            print(f"⚠️ 无方位描述，返回唯一匹配")
            return matches[0]
        else:
            print(f"⚠️ 无方位描述，返回最高得分匹配")
            return max(matches, key=lambda m: m["score"])

    def _find_reference_position(self, reference_text, all_texts_with_positions):
        """查找参考文本的位置
        
        采用三级匹配策略解决参考文本匹配问题：
        1. 精确子串匹配（忽略大小写）
        2. 部分字符匹配（检查字符包含比例，解决 'shya' vs 'hsya10' 问题）
        3. 模糊相似度匹配
        """
        import difflib
        
        print(f"🔍 [调试] 开始查找参考文本: '{reference_text}'")
        print(f"🔍 [调试] all_texts_with_positions 总数: {len(all_texts_with_positions)}")
        
        # 策略1：精确子串匹配（忽略大小写）
        ref_lower = reference_text.lower()
        for idx, item in enumerate(all_texts_with_positions):
            text = item["text"]
            text_lower = text.lower()
            contains_check = ref_lower in text_lower
            reverse_check = text_lower in ref_lower
            
            if contains_check or reverse_check:
                print(f"🎯 精确匹配找到参考文本[{idx+1}]: '{text}' -> '{reference_text}'")
                return item["position"]
        
        # 策略2：部分字符匹配（检查参考文本的字符是否大部分出现在目标文本中）
        # 这个策略专门解决 'shya' vs 'hsya10' 这类问题
        print(f"🔍 [调试] 精确匹配失败，尝试部分字符匹配...")
        best_partial_match = None
        best_char_ratio = 0.0
        char_ratio_threshold = 0.7  # 字符包含比例阈值70%
        
        for idx, item in enumerate(all_texts_with_positions):
            text = item["text"]
            text_lower = text.lower()
            
            # 计算参考文本中有多少字符出现在目标文本中
            matched_chars = sum(1 for char in ref_lower if char in text_lower)
            char_ratio = matched_chars / len(ref_lower) if len(ref_lower) > 0 else 0
            
            # 调试输出前几个高匹配度的文本
            if char_ratio > 0.5:
                print(f"🔍 [部分匹配{idx+1}] 文本: '{text}' | 字符匹配度: {char_ratio:.2f} ({matched_chars}/{len(ref_lower)})")
            
            if char_ratio > char_ratio_threshold and char_ratio > best_char_ratio:
                best_char_ratio = char_ratio
                best_partial_match = item
        
        if best_partial_match:
            print(f"🎯 部分字符匹配找到参考文本: '{best_partial_match['text']}' -> '{reference_text}' (字符匹配度: {best_char_ratio:.2f})")
            return best_partial_match["position"]
        
        # 策略3：模糊相似度匹配
        print(f"🔍 [调试] 部分字符匹配失败，尝试模糊相似度匹配...")
        best_fuzzy_match = None
        best_similarity = 0.0
        similarity_threshold = 0.5  # 降低相似度阈值到0.5
        
        for idx, item in enumerate(all_texts_with_positions):
            text = item["text"]
            # 计算文本相似度
            similarity = difflib.SequenceMatcher(None, ref_lower, text.lower()).ratio()
            
            # 调试输出前几个高相似度的文本
            if similarity > 0.3:
                print(f"🔍 [模糊匹配{idx+1}] 文本: '{text}' | 相似度: {similarity:.2f}")
            
            if similarity > similarity_threshold and similarity > best_similarity:
                best_similarity = similarity
                best_fuzzy_match = item
        
        if best_fuzzy_match:
            print(f"🎯 模糊匹配找到参考文本: '{best_fuzzy_match['text']}' -> '{reference_text}' (相似度: {best_similarity:.2f})")
            return best_fuzzy_match["position"]
        
        # 所有策略都失败，输出调试信息
        print(f"❌ 未找到参考文本 '{reference_text}'")
        print(f"📋 可用的OCR文本列表:")
        for i, item in enumerate(all_texts_with_positions[:10]):  # 只显示前10个避免日志过长
            print(f"   {i+1}. '{item['text']}' at {item['position']}")
        if len(all_texts_with_positions) > 10:
            print(f"   ... 还有 {len(all_texts_with_positions) - 10} 个文本")
        
        return None

    def _select_by_spatial_relation(self, matches, reference_position, relation):
        """根据空间关系选择最佳匹配"""
        ref_x, ref_y = reference_position
        
        valid_matches = []
        
        for match in matches:
            match_x, match_y = match["position"]
            
            # 根据关系筛选有效匹配
            if relation == "above" and match_y < ref_y:
                distance = ref_y - match_y
                valid_matches.append((match, distance))
            elif relation == "below" and match_y > ref_y:
                distance = match_y - ref_y
                valid_matches.append((match, distance))
            elif relation == "left" and match_x < ref_x:
                distance = ref_x - match_x
                valid_matches.append((match, distance))
            elif relation == "right" and match_x > ref_x:
                distance = match_x - ref_x
                valid_matches.append((match, distance))
        
        if valid_matches:
            # 选择距离最近的匹配
            best_match, min_distance = min(valid_matches, key=lambda x: x[1])
            print(f"🎯 选择距离最近的匹配 (距离: {min_distance}px)")
            return best_match
        else:
            print(f"⚠️ 没有符合空间关系 '{relation}' 的匹配")
            return None

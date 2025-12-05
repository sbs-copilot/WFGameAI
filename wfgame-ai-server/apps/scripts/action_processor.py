# -*- coding: utf-8 -*-
"""
Action处理器模块
负责处理JSON脚本中的各种action操作
"""

# 🔧 新增：禁用第三方库DEBUG日志
import logging

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

    def set_device_account(self, device_account):
        """设置设备账号信息"""
        self.device_account = device_account

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
        step_yolo_class = step.get("yolo_class")  # 修复: 确保step_yolo_class已定义
        print(f"[DEBUG] _process_action called: step_action={step_action}, step_yolo_class={step_yolo_class}, step_idx={step_idx}")
        
        # 获取重试配置(默认值: max_retries=3, retry_interval=1秒)
        max_retries = step.get("max_retries", 3)
        retry_interval = step.get("retry_interval", 1)
        
        # 预先初始化result变量，避免未赋值错误
        result = ActionResult(
            success=False,
            message="步骤未执行",
            details={"operation": step_action, "status": "not_executed"}
        )
        
        # 执行步骤(带重试机制)
        for attempt in range(max_retries):
            if attempt > 0:
                print(f"🔄 [步骤 {step_idx + 1}] 第 {attempt + 1}/{max_retries} 次重试...")
                time.sleep(retry_interval)
            
            result = self._execute_single_action(step, step_idx, log_dir, step_action, step_yolo_class)
            
            # 如果成功或者是不需要重试的操作,直接返回
            if result.success or step_action in ["delay", "log", "device_preparation"]:
                if attempt > 0:
                    print(f"✅ [步骤 {step_idx + 1}] 重试成功 (第 {attempt + 1} 次尝试)")
                break
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
            result = self._handle_input(step, step_idx)

        elif step_action == "checkbox":
            result = self._handle_checkbox(step, step_idx)

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
            elif "target_selector" in step:
                # 处理 target_selector 逻辑
                converted_step = step.copy()
                target_selector = converted_step["target_selector"]
                # 尝试从target_selector提取参数

                if target_selector.get("type"):
                    converted_step["ui_type"] = target_selector["type"]
                    converted_step["detection_method"] = "ui"
                del converted_step["target_selector"]
                return self._process_action(converted_step, step_idx, log_dir)
            else:
                # 默认处理：尝试AI检测点击

                if step_action == "fallback_click" and "relative_x" in step and "relative_y" in step:
                    result = self._handle_fallback_click(step, step_idx, log_dir)

                elif step_yolo_class and step_yolo_class != "fallback_click":
                    # 对于Priority模式脚本，如果有yolo_class字段，执行AI检测点击
                    print(f"🎯 检测到yolo_class字段: {step_yolo_class}，执行AI检测点击")
                    result = self._handle_ai_detection_click(step, step_idx, log_dir)
                elif step_yolo_class and step_yolo_class != "fallback_click":
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
        delay_seconds = step.get("params", {}).get("seconds", 1)
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
    
    def _handle_ocr_only_detection(self, step, step_idx, log_dir):
        """处理纯OCR检测模式(全屏搜索)"""
        ocr_keywords = step.get("ocr_keywords")
        ocr_min_score = step.get("ocr_min_score", 0.5)
        step_remark = step.get("remark", "")
        print(f"[OCR模式] 关键字: {ocr_keywords}, 最小置信度: {ocr_min_score}")
        
        try:
            screenshot = get_device_screenshot(self.device)
            if screenshot is None:
                print(f"❌ 无法获取设备屏幕截图")
                return self._create_failed_result(ocr_keywords, step_remark, "screenshot_failed")
            
            frame = cv2.cvtColor(np.array(screenshot), cv2.COLOR_RGB2BGR)
            
            # 调用replay_script中的全屏OCR检测函数
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
                print(f"❌ OCR未找到匹配的文本: {ocr_keywords}")
                return ActionResult(
                    success=False,
                    message=f"OCR未找到匹配的文本: {ocr_keywords}",
                    details={"operation": "ai_detection_click", "ocr_keywords": ocr_keywords},
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
        step_class = step.get("yolo_class")
        ocr_keywords = step.get("ocr_keywords")
        step_remark = step.get("remark", "")
        print(f"[YOLO+OCR模式] YOLO类别: {step_class}, OCR关键字: {ocr_keywords}")
        
        try:
            screenshot = get_device_screenshot(self.device)
            if screenshot is None:
                print(f"❌ 无法获取设备屏幕截图")
                return self._create_failed_result(f"{step_class}+{ocr_keywords}", step_remark, "screenshot_failed")
            
            frame = cv2.cvtColor(np.array(screenshot), cv2.COLOR_RGB2BGR)
            
            if self.detect_buttons:
                step_confidence = step.get("confidence", 0.35)
                ocr_min_score = step.get("ocr_min_score", 0.5)
                
                print(f"🎯 YOLO置信度: {step_confidence}, OCR最小置信度: {ocr_min_score}")
                
                # YOLO+OCR组合模式
                success, detection_result = self.detect_buttons(
                    frame,
                    target_class=step_class,
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
                        "target_class": step_class,
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
                            "desc": step_remark or f"YOLO+OCR检测点击({step_class}+{ocr_keywords})",
                            "executed": True
                        }
                    }
                    if screen_data:
                        ai_entry["data"]["screen"] = screen_data
                    self._write_log_entry(ai_entry)
                    
                    return ActionResult(
                        success=True,
                        message=f"YOLO+OCR检测点击成功: {step_class}",
                        details={"operation": "ai_detection_click", "target_class": step_class, "position": [int(x), int(y)]},
                        executed=True
                    )
                else:
                    print(f"❌ YOLO+OCR未找到匹配目标")
                    return ActionResult(
                        success=False,
                        message=f"YOLO+OCR未找到匹配目标: {step_class}+{ocr_keywords}",
                        details={"operation": "ai_detection_click", "target_class": step_class, "ocr_keywords": ocr_keywords},
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
            print(f"应用管理器2: {app_manager}")
            # 现在所有信息都在脚本中提供，直接使用package_name启动
            if package_name and app_manager:
                print(f"🔍 使用脚本中提供的包名直接启动: {package_name}")
                startup_success = app_manager.force_start_by_package(package_name, self.device.serial)
            else:
                print(f"❌ 缺少package_name参数或AppLifecycleManager不可用，无法启动应用")
                startup_success = False
            print(f"应用启动命令执行: {'成功' if startup_success else '失败'}")


            print(f"应用启动整体结果: {final_result}")

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
                    "ret": final_result,
                    "end_time": timestamp + 1
                }
            }
            # 添加screen对象到日志条目（如果可用）
            if screen_data:
                app_start_entry["data"]["screen"] = screen_data            # 添加 executed 字段到日志条目
            app_start_entry["data"]["executed"] = final_result

            self._write_log_entry(app_start_entry)

            # 修复: 根据实际结果返回正确的状态
            if final_result:
                print("✅ 应用启动和权限处理都成功")
                return ActionResult(
                    success=True,
                    message="应用启动和权限处理成功",
                    details={
                        "operation": "app_start",
                        "app_name": app_name,
                        "package_name": package_name,
                        "permission_handled": handle_permission
                    }
                )
            else:
                print("❌ 应用启动或权限处理失败")
                return ActionResult(
                    success=False,
                    message="应用启动或权限处理失败",
                    details={
                        "operation": "app_start",
                        "error": "startup_or_permission_failed"
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
        params = step.get("params", {})
        step_remark = step.get("remark", "")
        app_name = params.get("app_name", "")
        package_name = params.get("package_name", "")

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
        log_message = step.get("params", {}).get("message", step.get("remark", ""))
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
                        x, y, detected_class = detection_result
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
        """处理等待元素出现步骤 - 等待指定元素从无到有的出现过程"""
        # 解析参数，使用新的参数名称
        yolo_class = step.get("yolo_class", "")
        ui_type = step.get("ui_type", "")
        detection_method = step.get("detection_method", "ai" if yolo_class else "ui")

        step_remark = step.get("remark", "")
        max_wait = step.get("max_wait", 10)
        polling_interval = step.get("polling_interval", 1)
        confidence = step.get("confidence", 0.8)
        fail_on_timeout = step.get("fail_on_timeout", True)
        fallback_yolo_class = step.get("fallback_yolo_class", "")

        print(f"\n🚀 [步骤 {step_idx+1}] 开始执行 wait_for_appearance 操作")
        print(f"📋 检测方式: {detection_method}")
        if detection_method == "ai" and yolo_class:
            print(f"🎯 AI类别: '{yolo_class}'")
        elif detection_method == "ui" and ui_type:
            print(f"🎯 UI类型: '{ui_type}'")
        print(f"⏰ 超时时间: {max_wait}秒")
        print(f"🔄 轮询间隔: {polling_interval}秒")
        print(f"🎯 置信度: {confidence}")
        print(f"📝 备注: {step_remark}")
        print(f"⏱️ 步骤开始时间: {time.strftime('%H:%M:%S', time.localtime())}")

        wait_start_time = time.time()
        element_appeared = False
        wait_result = "not_appeared"
        detected_class = ""
        detection_result = None
        success = False  # 修复：初始化success变量，避免UnboundLocalError

        try:
            loop_count = 0
            while time.time() - wait_start_time < max_wait:
                loop_count += 1
                print(f"\n🔍 [循环 {loop_count}] 检查元素是否出现...")
                # 根据detection_method选择检测方式
                if detection_method == "ai" and yolo_class:
                    # AI检测方式
                    if self.detect_buttons:
                        # 获取当前屏幕截图
                        screenshot = get_device_screenshot(self.device)
                        if screenshot is None:
                            print("❌ 无法获取屏幕截图")
                            time.sleep(polling_interval)
                            continue

                        import cv2
                        import numpy as np
                        frame = cv2.cvtColor(np.array(screenshot), cv2.COLOR_RGB2BGR)

                        success, detection_result = self.detect_buttons(frame, target_class=yolo_class)
                        print(f"🔍 AI检测结果: success={success}, detection_result={detection_result}")

                        if success and detection_result[0] is not None:
                            element_appeared = True
                            x, y, detected_class = detection_result
                            wait_result = "appeared"
                            print(f"✅ [AI检测成功] 元素 '{yolo_class}' 已出现!")
                            print(f"📍 位置: ({x:.1f}, {y:.1f})")
                            print(f"🏷️ 检测类别: {detected_class}")
                            break
                    else:
                        print("⚠️ AI检测功能不可用")

                elif detection_method == "ui" and ui_type:
                    # UI检测方式
                    if DeviceScriptReplayer:
                        input_handler = DeviceScriptReplayer(self.device.serial)
                        target_selector = {"type": ui_type}
                        target_element = input_handler.find_element_smart(target_selector)

                        if target_element:
                            element_appeared = True
                            wait_result = "appeared"
                            bounds = target_element.get('bounds', '')
                            coords = input_handler._parse_bounds(bounds) if bounds else None
                            if coords:
                                x, y = coords
                                detection_result = (x, y, ui_type)
                            print(f"✅ [UI检测成功] 元素 '{ui_type}' 已出现!")
                            break
                    else:
                        print("⚠️ UI检测功能不可用")

                if element_appeared:
                    break

                print(f"⏳ [循环 {loop_count}] 元素尚未出现，等待 {polling_interval}秒后重试...")
                time.sleep(polling_interval)
                # 检查是否使用备选方案
            if not element_appeared and fallback_yolo_class and detection_method == "ui":
                print(f"\n🔄 UI检测失败，尝试备选AI检测: {fallback_yolo_class}")
                if self.detect_buttons:
                    # 获取当前屏幕截图
                    screenshot = get_device_screenshot(self.device)
                    if screenshot is not None:
                        import cv2
                        import numpy as np
                        frame = cv2.cvtColor(np.array(screenshot), cv2.COLOR_RGB2BGR)

                        success, detection_result = self.detect_buttons(frame, target_class=fallback_yolo_class)
                        if success and detection_result[0] is not None:
                            element_appeared = True
                            x, y, detected_class = detection_result
                            wait_result = "appeared"
                            print(f"✅ [备选AI检测成功] 元素已出现!")
                    else:
                        print("❌ 无法获取屏幕截图用于备选检测")

            total_wait_time = time.time() - wait_start_time

            # ====== 自动点击逻辑实现 ======
            auto_clicked = False
            click_position = None
            click_success = None
            click_error = None
            execute_action = step.get("execute_action", None)
            if element_appeared and execute_action == "click":
                try:
                    if detection_result and len(detection_result) >= 2 and detection_result[0] is not None and detection_result[1] is not None:
                        abs_x, abs_y = int(detection_result[0]), int(detection_result[1])
                        print(f"🤖 自动点击检测到的坐标: ({abs_x}, {abs_y})")
                        self.device.shell(f"input tap {abs_x} {abs_y}")
                        auto_clicked = True
                        click_position = (abs_x, abs_y)
                        click_success = True
                    else:
                        print("⚠️ 检测结果无有效坐标，无法自动点击")
                        click_success = False
                except Exception as ce:
                    print(f"❌ 自动点击异常: {ce}")
                    click_success = False
                    click_error = str(ce)

            if element_appeared:
                print(f"🎉 元素出现检测成功! 总等待时间: {total_wait_time:.1f}秒")
            else:
                wait_result = "timeout"
                print(f"⏰ 等待超时! 总等待时间: {total_wait_time:.1f}秒")

        except Exception as e:
            print(f"❌ 等待过程中发生异常: {e}")
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
                    "detection_method": detection_method,
                    "yolo_class": yolo_class,
                    "ui_type": ui_type,
                    "max_wait": max_wait,
                    "polling_interval": polling_interval,
                    "confidence": confidence,
                    "execute_action": execute_action
                },
                "start_time": wait_start_time,
                "ret": {
                    "element_appeared": element_appeared,
                    "wait_result": wait_result,
                    "total_wait_time": total_wait_time,
                    "detected_class": detected_class,
                    "auto_clicked": auto_clicked,
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

        # 添加 executed 字段到日志条目
        wait_entry["data"]["executed"] = success

        self._write_log_entry(wait_entry)

        # 返回统一的ActionResult对象
        success = element_appeared
        if not element_appeared and not fail_on_timeout:
            success = True  # 如果配置为忽略超时失败，则认为成功

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
                "detection_method": detection_method,
                "yolo_class": yolo_class,
                "ui_type": ui_type,
                "auto_clicked": auto_clicked,
                "click_position": click_position,
                "click_success": click_success,
                "click_error": click_error
            }
        )

    def _handle_wait_for_stable(self, step, step_idx, log_dir):
        """处理等待界面稳定步骤 - 等待界面连续N秒无变化，确保操作时机"""
        detection_method = step.get("detection_method", "ai")
        step_remark = step.get("remark", "")
        duration = step.get("duration", 2)
        max_wait = step.get("max_wait", 10)
        check_structure = step.get("check_structure", True)
        check_positions = step.get("check_positions", True)
        tolerance = step.get("tolerance", 0.05)
        ignore_animations = step.get("ignore_animations", True)

        print(f"\n🚀 [步骤 {step_idx+1}] 开始执行 wait_for_stable 操作")
        print(f"📋 检测方式: {detection_method}")
        print(f"🎯 稳定持续时间: {duration}秒")
        print(f"⏰ 最大等待时间: {max_wait}秒")
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
            while time.time() - wait_start_time < max_wait:
                current_time = time.time()

                # 获取当前状态
                current_screenshot = None
                current_ui_structure = None
                if detection_method == "ai":
                    # 使用截图比较检测稳定性
                    try:
                        import subprocess
                        screenshot_result = subprocess.run(
                            f"adb -s {self.device.serial} exec-out screencap -p",
                            shell=True, capture_output=True
                        )
                        if screenshot_result.returncode == 0:
                            import cv2
                            import numpy as np
                            # 将字节数据转换为图像
                            nparr = np.frombuffer(screenshot_result.stdout, np.uint8)
                            current_screenshot = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
                    except Exception as e:
                        print(f"⚠️ 截图获取失败: {e}")

                elif detection_method == "ui":
                    # 使用UI结构比较检测稳定性
                    if DeviceScriptReplayer:
                        input_handler = DeviceScriptReplayer(self.device.serial)
                        current_ui_structure = input_handler.get_ui_hierarchy()

                # 检查是否与上次状态相同
                is_same = False

                if detection_method == "ai" and current_screenshot is not None:
                    if last_screenshot is not None:
                        # 计算图像差异
                        diff = cv2.absdiff(current_screenshot, last_screenshot)
                        diff_ratio = np.sum(diff) / (diff.shape[0] * diff.shape[1] * diff.shape[2] * 255)
                        is_same = diff_ratio < tolerance
                        print(f"🖼️ 截图差异比例: {diff_ratio:.4f} (阈值: {tolerance})")
                    else:
                        print("📸 获取第一张参考截图")
                    last_screenshot = current_screenshot.copy() if current_screenshot is not None else None

                elif detection_method == "ui" and current_ui_structure:
                    if last_ui_structure:
                        # 简单的字符串比较（可以优化为结构化比较）
                        is_same = current_ui_structure == last_ui_structure
                        print(f"🏗️ UI结构相同: {is_same}")
                    else:
                        print("🏗️ 获取第一个参考UI结构")
                    last_ui_structure = current_ui_structure

                # 更新稳定状态
                if is_same:
                    if stable_start_time is None:
                        stable_start_time = current_time
                        print(f"🟢 界面开始稳定...")

                    stable_duration = current_time - stable_start_time
                    print(f"⏱️ 已稳定 {stable_duration:.1f}/{duration}秒")

                    if stable_duration >= duration:
                        is_stable = True
                        stability_result = "stable"
                        print(f"✅ 界面已稳定 {duration}秒!")
                        break
                else:
                    if stable_start_time is not None:
                        print(f"🔄 界面发生变化，重新开始计时")
                    stable_start_time = None

                time.sleep(0.5)  # 检查间隔

            total_wait_time = time.time() - wait_start_time

            if not is_stable:
                stability_result = "timeout"
                print(f"⏰ 等待稳定超时! 总等待时间: {total_wait_time:.1f}秒")
            else:
                print(f"🎉 界面稳定检测成功! 总等待时间: {total_wait_time:.1f}秒")

        except Exception as e:
            print(f"❌ 稳定检测过程中发生异常: {e}")
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
                    "detection_method": detection_method,
                    "duration": duration,
                    "max_wait": max_wait,
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
                "detection_method": detection_method,
                "duration": duration,
                "max_wait": max_wait
            }
        )

    def _handle_retry_until_success(self, step, step_idx, log_dir):
        """处理重试直到成功步骤 - 对任意操作进行重试，直到成功或达到最大次数"""        # 解析参数
        detection_method = step.get("detection_method", "ai")
        execute_action = step.get("execute_action",  "click")
        yolo_class = step.get("yolo_class",  "")
        ui_type = step.get("ui_type",  "")
        text = step.get("text",  "")
        step_remark = step.get("remark", "")

        max_retries = step.get("max_retries", 5)
        retry_interval = step.get("retry_interval", 1)
        polling_interval = step.get("polling_interval", retry_interval)
        initial_delay = step.get("initial_delay", 1)
        max_delay = step.get("max_delay", 10)
        backoff_multiplier = step.get("backoff_multiplier", 2)
        verify_success = step.get("verify_success", False)
        stop_on_success = step.get("stop_on_success", True)
        print(f"\n🚀 [步骤 {step_idx+1}] 开始执行 retry_until_success 操作")
        print(f"📋 检测方式: {detection_method}")
        print(f"🎯 执行操作: {execute_action}")
        if detection_method == "ai" and yolo_class:
            print(f"🎯 AI类别: '{yolo_class}'")
        elif detection_method == "ui" and ui_type:
            print(f"🎯 UI类型: '{ui_type}'")
        print(f"🔄 最大重试次数: {max_retries}")
        print(f"📝 备注: {step_remark}")

        retry_start_time = time.time()
        success = False
        last_error = None
        retry_count = 0
        current_delay = initial_delay
        for attempt in range(max_retries + 1):  # +1 为第一次尝试
            retry_count = attempt
            print(f"\n🔄 [尝试 {attempt + 1}/{max_retries + 1}] 执行 {execute_action} 操作...")

            try:
                # 构造重试操作的step
                retry_step = {
                    "action": execute_action,
                    "detection_method": detection_method,
                    "yolo_class": yolo_class,
                    "ui_type": ui_type,
                    "text": text,
                    "remark": f"重试操作 {attempt + 1}: {step_remark}"
                }                # 根据execute_action执行相应操作
                operation_success = False
                if execute_action == "click":
                    if detection_method == "ai" and yolo_class:
                        # AI检测点击
                        if self.detect_buttons:
                            # 获取当前屏幕截图
                            screenshot = get_device_screenshot(self.device)
                            if screenshot is None:
                                print("❌ 无法获取屏幕截图")
                                continue

                            import cv2
                            import numpy as np
                            frame = cv2.cvtColor(np.array(screenshot), cv2.COLOR_RGB2BGR)

                            ai_success, detection_result = self.detect_buttons(frame, target_class=yolo_class)
                            if ai_success and detection_result[0] is not None:
                                x, y, detected_class = detection_result
                                self.device.shell(f"input tap {int(x)} {int(y)}")
                                operation_success = True
                                print(f"✅ AI点击成功: ({x:.1f}, {y:.1f})")
                    elif detection_method == "ui" and ui_type:
                        # UI检测点击                        if DeviceScriptReplayer:
                            input_handler = DeviceScriptReplayer(self.device.serial)
                            target_selector = {"type": ui_type}
                            operation_success = input_handler.perform_click_action(target_selector)
                            if operation_success:
                                print(f"✅ UI点击成功")

                elif execute_action == "input":
                    # 文本输入操作
                    # 🔧 重要修复：在retry中也需要进行参数化替换
                    input_text = text

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
                            continue

                    if "${account:password}" in input_text:
                        if self.device_account and len(self.device_account) >= 2:
                            input_text = input_text.replace("${account:password}", self.device_account[1])
                            print(f"✅ 替换密码参数")
                        else:
                            device_serial = getattr(self.device, 'serial', self.device_name)
                            print(f"❌ 错误: 设备 {device_serial} 没有分配账号，无法替换密码参数")
                            continue

                    # 支持AI定位的输入框后聚焦输入
                    if detection_method == "ai" and yolo_class:
                        if self.detect_buttons:
                            screenshot = get_device_screenshot(self.device)
                            if screenshot is None:
                                print("❌ 无法获取屏幕截图")
                            else:
                                import cv2
                                import numpy as np
                                frame = cv2.cvtColor(np.array(screenshot), cv2.COLOR_RGB2BGR)
                                ai_ok, detection_result = self.detect_buttons(frame, target_class=yolo_class)
                                if ai_ok and detection_result[0] is not None:
                                    x, y = detection_result[0], detection_result[1]
                                    # 点击输入框聚焦
                                    self.device.shell(f"input tap {int(x)} {int(y)}")
                                    time.sleep(0.5)
                                    # 输入文本 - 使用替换后的input_text
                                    escaped_text = input_text.replace(' ', '%s').replace("'", "\\'")
                                    self.device.shell(f"input text '{escaped_text}'")
                                    operation_success = True
                                    print(f"✅ AI文本输入成功: {input_text}")                    # 兼容UI方式输入
                    elif detection_method == "ui" and ui_type:
                        if DeviceScriptReplayer:
                            input_handler = DeviceScriptReplayer(self.device.serial)
                            target_selector = {"type": ui_type}
                            # 先找到输入框
                            target_element = input_handler.find_element_smart(target_selector)
                            if target_element:
                                # 点击获取焦点
                                if input_handler.tap_element(target_element):
                                    time.sleep(0.5)
                                    # 输入文本 - 使用替换后的input_text
                                    escaped_text = input_text.replace(' ', '%s').replace("'", "\\'")
                                    self.device.shell(f"input text '{escaped_text}'")
                                    operation_success = True
                                    print(f"✅ 文本输入成功")

                elif execute_action == "checkbox":
                    # 复选框操作
                    if detection_method == "ui" and ui_type:
                        if DeviceScriptReplayer:
                            input_handler = DeviceScriptReplayer(self.device.serial)
                            target_selector = {"type": ui_type}
                            operation_success = input_handler.perform_checkbox_action(target_selector)
                            if operation_success:
                                print(f"✅ 复选框操作成功")

                # 验证操作成功（如果启用）
                if operation_success and verify_success:
                    print("🔍 验证操作结果...")
                    time.sleep(1)  # 等待UI响应
                    # 可以在这里添加更复杂的验证逻辑
                    # 目前简单假设操作成功

                if operation_success:
                    success = True
                    print(f"🎉 [尝试 {attempt + 1}] 操作成功!")
                    if stop_on_success:
                        break
                else:
                    print(f"❌ [尝试 {attempt + 1}] 操作失败")

            except Exception as e:
                print(f"❌ [尝试 {attempt + 1}] 操作异常: {e}")
                last_error = str(e)

            # 如果还有重试机会，计算延迟时间
            if attempt < max_retries and not (success and stop_on_success):
                delay = polling_interval

                print(f"⏳ 等待 {delay}秒后重试...")
                time.sleep(delay)

        total_retry_time = time.time() - retry_start_time

        if success:
            print(f"🎉 重试操作最终成功! 尝试次数: {retry_count + 1}, 总时间: {total_retry_time:.1f}秒")
        else:
            print(f"❌ 重试操作最终失败! 尝试次数: {retry_count + 1}, 总时间: {total_retry_time:.1f}秒")

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
                    "detection_method": detection_method,
                    "execute_action": execute_action,
                    "yolo_class": yolo_class,
                    "ui_type": ui_type,
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
                "execute_action": execute_action,
                "detection_method": detection_method
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
        max_wait: float = 5.0,
        retry_interval: float = 0.5,
        duration: float = 1.0
    ) -> bool:
        """
        检查并自动处理系统弹窗。
        参数:
            max_wait: 最多等待时间（秒）
            retry_interval: 每次检测间隔（秒）
            duration: 点击后等待弹窗消失时间（秒）
        """
        import time
        start = time.time()
        handled = False

        while time.time() - start < max_wait:
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

            # 兼容两种脚本格式
            # 格式1: 数组格式 [{step1}, {step2}] - 前端脚本编辑器使用
            # 格式2: 对象格式 {"defaults": {...}, "steps": [...]} - 精简版本使用
            if isinstance(script_json, list):
                # 数组格式
                print("📋 检测到数组格式脚本")
                steps = script_json
                defaults = {}
                meta = {}
                
                # 检查第一个步骤是否是defaults定义
                if len(steps) > 0:
                    first_step = steps[0]
                    # 支持多种标记方式
                    is_defaults_step = False
                    exclude_keys = []
                    
                    if first_step.get('step_type') == 'defaults':
                        # 方式1: step_type="defaults" (推荐)
                        is_defaults_step = True
                        exclude_keys = ['step_type', 'remark']
                    elif first_step.get('is_defaults') == True:
                        # 方式2: is_defaults=true
                        is_defaults_step = True
                        exclude_keys = ['is_defaults', 'remark']
                    elif first_step.get('action') == 'set_defaults':
                        # 方式3: action="set_defaults"
                        # 这种方式下,action字段本身是标记,真正的action应该在default_action字段
                        is_defaults_step = True
                        exclude_keys = ['action', 'remark']
                        # 如果有default_action字段,将其重命名为action
                        if 'default_action' in first_step:
                            first_step['action'] = first_step['default_action']
                            exclude_keys.append('default_action')
                    
                    if is_defaults_step:
                        # 第一个步骤是defaults定义,提取所有参数(排除标记字段)
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
                step_class = step.get('class', '')
                target_selector = step.get('target_selector', {})
                text = step.get('text', '')
                params = step.get('params', {})
                remark = step.get('remark', '')

                # 处理ui_type字段，将其转换为target_selector格式
                ui_type = step.get('ui_type')
                if ui_type and not target_selector:
                    target_selector = {'type': ui_type}
                elif ui_type and target_selector and 'type' not in target_selector:
                    target_selector['type'] = ui_type

                # 如果没有action字段，根据class字段推导action
                if not action:
                    if step_class in ['app_start', 'start_app']:
                        action = 'app_start'
                    elif step_class in ['app_stop', 'stop_app']:
                        action = 'app_stop'
                    elif step_class in ['device_preparation']:
                        action = 'device_preparation'
                    elif step_class in ['delay', 'wait', 'sleep']:
                        action = 'delay'
                    elif step_class:  # 如果有class但没有action，默认为click
                        action = 'click'
                    else:
                        action = 'click'  # 完全默认为点击
                        print(f"🔧 执行步骤 {step_idx + 1}: action={action}, remark={remark}")                # 读取弹窗处理参数（仅当JSON脚本中明确设置时才执行）
                # 优先级：step参数 > meta参数，如果都没有设置则不执行弹窗处理
                step_auto_handle = step.get('auto_handle_dialog')
                meta_auto_handle = meta.get('auto_handle_dialog')

                # 只有当step或meta中明确设置了auto_handle_dialog参数时才处理弹窗
                if step_auto_handle is not None:
                    auto_handle = step_auto_handle
                elif meta_auto_handle is not None:
                    auto_handle = meta_auto_handle
                else:
                    auto_handle = False  # 如果都没设置，默认不处理弹窗

                # 步骤前处理弹窗（仅当明确启用时）
                if auto_handle:
                    max_wait = step.get('dialog_max_wait', meta.get('dialog_max_wait', 5.0))
                    retry_interval = step.get('dialog_retry_interval', meta.get('dialog_retry_interval', 0.5))
                    duration = step.get('dialog_duration', meta.get('dialog_duration', 1.0))

                    print(f"🛡️ 检测并处理系统弹窗（最大等待：{max_wait}秒）")
                    self.handle_system_dialogs(
                        max_wait=max_wait,
                        retry_interval=retry_interval,
                        duration=duration
                    )

                try:
                    if action == 'delay':
                        # 延迟操作
                        delay_time = params.get('seconds', 1.0)
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
                        print(f"☑️ 执行checkbox勾选操作")
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

                    # 操作间延迟
                    time.sleep(0.5)
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

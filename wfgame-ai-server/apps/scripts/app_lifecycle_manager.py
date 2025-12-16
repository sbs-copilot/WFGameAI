"""
应用生命周期管理器 - 负责应用的启动、停止和状态管理
"""
import subprocess
import time
import os
import json
import logging
import re
from typing import Optional, Dict, List, Union, Tuple, Any
from enum import Enum
from dataclasses import dataclass, asdict


# 配置日志
logging.basicConfig(level=logging.INFO,
                   format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class AppState(Enum):
    """应用状态枚举"""
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    ERROR = "error"
    UNKNOWN = "unknown"

@dataclass
class AppInstance:
    """应用实例信息"""
    package_name: str
    device_serial: str
    pid: Optional[int] = None
    state: AppState = AppState.STOPPED
    start_time: Optional[float] = None
    last_check_time: Optional[float] = None
    error_message: Optional[str] = None
    restart_count: int = 0

@dataclass
class AppTemplate:
    """应用模板配置"""
    name: str
    package_name: str
    activity_name: Optional[str] = None
    start_commands: Optional[List[str]] = None
    stop_commands: Optional[List[str]] = None
    check_commands: Optional[List[str]] = None
    startup_wait_time: int = 5
    max_restart_attempts: int = 3
    health_check_interval: int = 30
    custom_params: Optional[Dict[str, Any]] = None

    def __post_init__(self):
        if self.start_commands is None:
            self.start_commands = []
        if self.stop_commands is None:
            self.stop_commands = []
        if self.check_commands is None:
            self.check_commands = []
        if self.custom_params is None:
            self.custom_params = {}



class AppLifecycleManager:
    """应用生命周期管理器，提供统一的应用启动、停止和状态检查接口"""

    def __init__(self):
        """初始化应用生命周期管理器"""
        # 模板配置目录
        self.templates_dir = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "app_templates"
        )

        # 确保模板目录存在
        os.makedirs(self.templates_dir, exist_ok=True)

        # 缓存已加载的模板
        self._template_cache = {}

        # 运行中的应用实例
        self.app_instances: Dict[str, AppInstance] = {}  # key: f"{device_serial}_{package_name}"

    def start_app(self, app_name_or_package: str, device_id: str) -> bool:
        """
        启动应用

        Args:
            app_name_or_package: 应用模板名称或包名
            device_id: 设备ID

        Returns:
            bool: 启动是否成功
        """
        logger.info(f"尝试启动应用: {app_name_or_package} 在设备 {device_id}")

        # 尝试作为模板名称处理
        template = self._load_template(app_name_or_package)
        if template:
            package_name = template.get('package_name')
            main_activity = template.get('main_activity', '.MainActivity')
            logger.info(f"使用模板启动应用: {package_name}/{main_activity}")
        else:
            # 作为包名直接使用
            package_name = app_name_or_package
            main_activity = '.MainActivity'
            logger.info(f"直接使用包名启动应用: {package_name}/{main_activity}")

        if not package_name:
            logger.error(f"无法启动应用: {app_name_or_package}，未找到包名")
            return False

        # 确保主活动名称格式正确
        if main_activity and not main_activity.startswith('.') and not '/' in main_activity:
            main_activity = f".{main_activity}"

        # 组装完整组件名
        component = f"{package_name}/{main_activity}" if main_activity else package_name

        # 执行启动命令
        try:
            cmd = f"adb -s {device_id} shell am start -n {component}"
            # logger.info(f"执行命令: {cmd}")
            result = subprocess.run(
                cmd.split(),
                capture_output=True,
                text=True,
                timeout=30
            )

            # 检查命令输出
            if result.returncode == 0:
                logger.info(f"应用启动命令执行成功: {result.stdout.strip()}")

                # 等待应用启动并验证
                time.sleep(1)  # 给应用启动一点时间

                # 验证应用是否成功运行
                if self.check_app_running(package_name, device_id):
                    logger.info(f"应用成功启动并运行中: {package_name}")
                    return True
                else:
                    logger.warning(f"应用启动命令成功但应用未运行: {package_name}")
                    return False
            else:
                logger.error(f"应用启动命令失败: {result.stderr.strip()}")
                return False

        except subprocess.TimeoutExpired:
            logger.error(f"启动应用超时: {package_name}")
            return False
        except Exception as e:
            logger.error(f"启动应用出错: {e}")
            return False

    def stop_app(self, app_name_or_package: str, device_id: str) -> bool:
        """
        停止应用

        Args:
            app_name_or_package: 应用模板名称或包名
            device_id: 设备ID

        Returns:
            bool: 停止是否成功
        """
        logger.info(f"尝试停止应用: {app_name_or_package} 在设备 {device_id}")

        # 尝试作为模板名称处理
        template = self._load_template(app_name_or_package)
        if template:
            package_name = template.get('package_name')
            logger.info(f"使用模板停止应用: {package_name}")
        else:
            # 作为包名直接使用
            package_name = app_name_or_package
            logger.info(f"直接使用包名停止应用: {package_name}")

        if not package_name:
            logger.error(f"无法停止应用: {app_name_or_package}，未找到包名")
            return False

        # 执行强制停止命令
        return self.force_stop_by_package(package_name, device_id)

    def force_stop_by_package(self, package_name: str, device_id: str) -> bool:
        """
        通过包名强制停止应用

        Args:
            package_name: 应用包名
            device_id: 设备ID

        Returns:
            bool: 停止是否成功
        """
        logger.info(f"强制停止应用: {package_name} 在设备 {device_id}")

        try:
            # 先检查应用是否正在运行
            if not self._check_app_running_by_package(package_name, device_id):
                logger.info(f"应用 {package_name} 在设备 {device_id} 上未运行，无需停止")
                return True

            cmd = f"adb -s {device_id} shell am force-stop {package_name}"
            # logger.info(f"执行命令: {cmd}")
            result = subprocess.run(
                cmd.split(),
                capture_output=True,
                text=True,
                timeout=10
            )

            # 检查命令执行是否成功
            if result.returncode == 0:
                logger.info(f"应用停止命令执行成功，等待验证")

                # 等待应用完全停止并进行多次验证
                max_verify_attempts = 5
                for attempt in range(max_verify_attempts):
                    time.sleep(1)  # 等待停止生效

                    if not self._check_app_running_by_package(package_name, device_id):
                        logger.info(f"已确认应用 {package_name} 已停止 (第 {attempt + 1} 次验证)")
                        return True
                    else:
                        logger.warning(f"第 {attempt + 1} 次验证: 应用 {package_name} 仍在运行，继续等待...")

                # 所有验证都失败，尝试强制杀进程
                logger.warning(f"应用 {package_name} 仍在运行，尝试强制杀进程")
                if self._force_kill_app_processes(package_name, device_id):
                    # 最后一次验证
                    time.sleep(2)
                    if not self._check_app_running_by_package(package_name, device_id):
                        logger.info(f"强制杀进程后，应用 {package_name} 已停止")
                        return True

                logger.error(f"无法停止应用 {package_name}，应用可能被系统保护或有自启动机制")
                return False
            else:
                logger.error(f"应用停止命令失败: {result.stderr.strip()}")
                return False

        except subprocess.TimeoutExpired:
            logger.error(f"停止应用超时: {package_name}")
            return False
        except Exception as e:
            logger.error(f"停止应用出错: {e}")
            return False

    def check_app_running(self, package_name: str, device_id: str) -> bool:
        """
        检查应用是否正在运行

        Args:
            package_name: 应用包名
            device_id: 设备ID

        Returns:
            bool: 应用是否在运行        """
        logger.info(f"检查应用是否运行: {package_name} 在设备 {device_id}")

        try:
            # 方法1: 使用dumpsys activity检查应用进程 (Windows兼容)
            cmd = ["adb", "-s", device_id, "shell", "dumpsys", "activity", "processes"]
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=10
            )

            if result.returncode == 0 and package_name in result.stdout:
                logger.info(f"通过进程检查确认应用正在运行: {package_name}")
                return True            # 方法2: 检查应用当前活动 (Windows兼容)
            cmd = ["adb", "-s", device_id, "shell", "dumpsys", "activity", "activities"]
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=10
            )

            if result.returncode == 0 and package_name in result.stdout:
                logger.info(f"通过活动检查确认应用正在运行: {package_name}")
                return True
            # 方法3: 使用ps命令检查进程 (Windows兼容)
            cmd = ["adb", "-s", device_id, "shell", "ps", "-A"]
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=10
            )

            if result.returncode == 0 and package_name in result.stdout:
                logger.info(f"通过ps命令确认应用正在运行: {package_name}")
                return True

            logger.info(f"应用未运行: {package_name}")
            return False

        except subprocess.TimeoutExpired:
            logger.error(f"检查应用状态超时: {package_name}")
            return False
        except Exception as e:
            logger.error(f"检查应用状态出错: {e}")
            return False

    def get_running_apps(self, device_id: str) -> List[str]:
        """
        获取设备上当前运行的所有应用包名

        Args:
            device_id: 设备ID

        Returns:
            List[str]: 运行中应用的包名列表
        """
        logger.info(f"获取设备运行中的应用: {device_id}")

        running_apps = []

        try:
            # 使用dumpsys activity processes获取所有运行的应用 (Windows兼容)
            cmd = ["adb", "-s", device_id, "shell", "dumpsys", "activity", "processes"]
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=10
            )

            if result.returncode == 0:
                # 匹配所有 ProcessRecord 中的包名
                # 格式: ProcessRecord{xxx pid:package_name/xxx}
                pattern = re.compile(r'ProcessRecord\{[^}]+ \d+:([\w.]+)[:/]')
                matches = pattern.findall(result.stdout)

                # 过滤系统应用和去重
                running_apps = list(set([pkg for pkg in matches if not pkg.startswith('android.')
                              and not pkg.startswith('com.android.')
                              and not pkg.startswith('com.google.')]))

                logger.info(f"检测到运行中的应用: {len(running_apps)} 个")
                if running_apps:
                    logger.info(f"运行中的应用列表: {running_apps}")
            else:
                logger.error(f"获取运行中应用失败: {result.stderr.strip()}")

        except Exception as e:
            logger.error(f"获取运行中应用出错: {e}")
        return running_apps

    def _load_template(self, template_name: str) -> Optional[Dict[str, Any]]:
        """
        加载应用模板

        Args:
            template_name: 模板名称

        Returns:
            Optional[Dict[str, Any]]: 模板数据，如果找不到模板则返回None
        """
        # 如果有缓存，直接返回
        if template_name in self._template_cache:
            return self._template_cache[template_name]

        # 尝试加载模板文件 - 首先在默认路径
        template_path = os.path.join(self.templates_dir, f"{template_name}.json")

        # 如果默认路径不存在，尝试在scripts目录查找
        if not os.path.exists(template_path):
            scripts_template_dir = os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                "apps", "scripts", "app_templates"
            )
            template_path = os.path.join(scripts_template_dir, f"{template_name}.json")

            if os.path.exists(template_path):
                logger.info(f"在脚本目录找到模板文件: {template_path}")
            else:
                logger.warning(f"模板文件不存在: {template_name}.json (已尝试两个路径)")
                return None

        try:
            with open(template_path, 'r', encoding='utf-8') as f:
                template = json.load(f)

            # 缓存模板
            self._template_cache[template_name] = template
            return template
        except Exception as e:
            logger.error(f"加载模板失败: {e}")
            return None

    def create_template(self, template_name: str, package_name: str,
                       main_activity: str = '.MainActivity',
                       description: str = '') -> bool:
        """
        创建应用模板

        Args:
            template_name: 模板名称
            package_name: 应用包名
            main_activity: 应用主活动名称
            description: 模板描述

        Returns:
            bool: 创建是否成功
        """
        logger.info(f"创建应用模板: {template_name}")

        template = {
            'name': template_name,
            'package_name': package_name,
            'main_activity': main_activity,
            'description': description,
            'created_at': time.strftime('%Y-%m-%d %H:%M:%S')
        }

        template_path = os.path.join(self.templates_dir, f"{template_name}.json")
        try:
            with open(template_path, 'w', encoding='utf-8') as f:
                json.dump(template, f, indent=2, ensure_ascii=False)

            # 更新缓存
            self._template_cache[template_name] = template
            logger.info(f"应用模板创建成功: {template_path}")
            return True
        except Exception as e:
            logger.error(f"创建应用模板失败: {e}")
            return False

    def get_app_templates(self) -> List[Dict[str, Any]]:
        """
        获取所有应用模板

        Returns:
            List[Dict[str, Any]]: 模板列表
        """
        templates = []

        try:
            # 确保目录存在
            if not os.path.exists(self.templates_dir):
                return []

            # 加载所有模板文件
            for filename in os.listdir(self.templates_dir):
                if filename.endswith('.json'):
                    template_path = os.path.join(self.templates_dir, filename)
                    try:
                        with open(template_path, 'r', encoding='utf-8') as f:
                            template = json.load(f)

                        templates.append(template)

                        # 更新缓存
                        template_name = os.path.splitext(filename)[0]
                        self._template_cache[template_name] = template
                    except Exception as e:
                        logger.error(f"加载模板文件失败 {filename}: {e}")
        except Exception as e:
            logger.error(f"获取应用模板失败: {e}")

        return templates

    def force_start_by_package(self, package_name: str, device_serial: str, activity_name: Optional[str] = None) -> bool:
        """
        直接通过包名启动应用

        Args:
            package_name: 应用包名
            device_serial: 设备序列号
            activity_name: Activity名称（可选）

        Returns:
            bool: 启动是否成功
        """
        try:
            
            # 先停止应用
            self.force_stop_by_package(package_name, device_serial)
            time.sleep(1)

            logger.info(f"启动应用包 {package_name} 在设备 {device_serial}")

            # 构建启动命令
            if activity_name:
                start_command = f"adb -s {device_serial} shell am start -n {package_name}/{activity_name}"
            else:
                # 使用monkey启动应用
                start_command = f"adb -s {device_serial} shell monkey -p {package_name} -c android.intent.category.LAUNCHER 1"

            # 执行启动命令
            result = subprocess.run(
                start_command.split(),
                capture_output=True,
                text=True,
                timeout=30
            )

            if result.returncode == 0:
                logger.info(f"应用包 {package_name} 在设备 {device_serial} 上启动成功")

                # 创建或更新实例状态
                instance_key = f"{device_serial}_{package_name}"
                self.app_instances[instance_key] = AppInstance(
                    package_name=package_name,
                    device_serial=device_serial,
                    state=AppState.RUNNING,
                    start_time=time.time()
                )

                return True
            else:
                logger.error(f"启动应用包 {package_name} 失败: {result.stderr}")
                return False

        except Exception as e:
            logger.error(f"启动应用包 {package_name} 时发生异常: {e}")
            return False

    def _check_app_running_by_package(self, package_name: str, device_serial: str) -> bool:
        """
        通过包名检查应用是否正在运行（增强版本，多种方法验证）

        Args:
            package_name: 应用包名
            device_serial: 设备序列号

        Returns:
            bool: 应用是否正在运行
        """
        try:
            # 方法1: 使用dumpsys activity检查应用进程
            cmd = ["adb", "-s", device_serial, "shell", "dumpsys", "activity", "processes"]
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=8
            )

            if result.returncode == 0 and package_name in result.stdout:
                logger.debug(f"方法1确认应用正在运行: {package_name}")
                return True

            # 方法2: 使用ps命令检查进程
            cmd = ["adb", "-s", device_serial, "shell", "ps", "-A"]
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=8
            )

            if result.returncode == 0 and package_name in result.stdout:
                logger.debug(f"方法2确认应用正在运行: {package_name}")
                return True

            # 方法3: 使用pidof命令检查进程
            cmd = ["adb", "-s", device_serial, "shell", "pidof", package_name]
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=5
            )

            if result.returncode == 0 and result.stdout.strip():
                logger.debug(f"方法3确认应用正在运行: {package_name}, PID: {result.stdout.strip()}")
                return True

            return False

        except Exception as e:
            logger.warning(f"检查应用运行状态异常: {e}")
            return True  # 异常时假设应用仍在运行，避免误判

    def _force_kill_app_processes(self, package_name: str, device_serial: str) -> bool:
        """
        强制杀死应用的所有进程

        Args:
            package_name: 应用包名
            device_serial: 设备序列号

        Returns:
            bool: 是否成功杀死进程
        """
        try:
            logger.warning(f"强制杀死应用 {package_name} 的所有进程")

            # 先获取应用的所有PID
            cmd = ["adb", "-s", device_serial, "shell", "pidof", package_name]
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=5
            )

            if result.returncode == 0 and result.stdout.strip():
                pids = result.stdout.strip().split()
                logger.info(f"找到应用 {package_name} 的进程PID: {pids}")

                # 逐个杀死进程
                for pid in pids:
                    try:
                        kill_cmd = ["adb", "-s", device_serial, "shell", "kill", "-9", pid]
                        kill_result = subprocess.run(
                            kill_cmd,
                            capture_output=True,
                            text=True,
                            timeout=5
                        )
                        if kill_result.returncode == 0:
                            logger.warning(f"成功杀死进程 PID: {pid}")
                        else:
                            logger.warning(f"杀死进程 PID {pid} 失败: {kill_result.stderr}")
                    except Exception as e:
                        logger.warning(f"杀死进程 PID {pid} 异常: {e}")

                return True
            else:
                logger.warning(f"未找到应用 {package_name} 的进程")
                return True  # 没有进程也算成功

        except Exception as e:
            logger.error(f"强制杀死应用进程异常: {e}")
            return False

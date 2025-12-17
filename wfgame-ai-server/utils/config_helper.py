import json
import os

# 完全抑制日志输出
import logging
import sys
import warnings

warnings.filterwarnings("ignore")
logging.getLogger(__name__).setLevel(logging.ERROR)
logging.getLogger().setLevel(logging.ERROR)

# 修复编码问题
if sys.platform == "win32":
    try:
        if hasattr(sys.stdout, 'reconfigure'):
            sys.stdout.reconfigure(encoding="utf-8", errors="ignore")
            sys.stderr.reconfigure(encoding="utf-8", errors="ignore")
    except:
        pass
import configparser
from pathlib import Path
import logging
import glob
import glob
import shutil


from dataclasses import dataclass
from typing import Optional


# 减少日志输出，只在ERROR级别输出
logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)
logger.setLevel(logging.WARNING)

class ConfigManager:
    """
    统一配置管理类，用于处理config.ini中的所有路径配置
    """
    _instance = None
    _config = None
    # 修复：使用当前文件所在目录向上查找项目根目录
    # utils/config_helper.py -> wfgame-ai-server -> 项目根目录
    _config_path = os.path.join(Path(__file__).resolve().parent.parent.parent, "config.ini")

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(ConfigManager, cls).__new__(cls)
            cls._instance._load_config()
        return cls._instance

    def _load_config(self):
        """加载配置文件"""
        self._config = configparser.ConfigParser(interpolation=configparser.ExtendedInterpolation())
        self._config_path = self._find_config_file()
        if self._config_path and os.path.exists(self._config_path):
            self._config.read(self._config_path, encoding='utf-8')
            # logger.warning(f"ConfigManager 已加载配置文件: {self._config_path}")
        else:
            raise FileNotFoundError(f"无法找到 {self._config_path} 配置文件")

    def _find_config_file(self):
        """查找配置文件 - 优先使用环境变量WFGAMEAI_CONFIG指定的配置文件"""
        # 优先检查 WFGAMEAI_CONFIG 环境变量
        config_from_env = os.environ.get("WFGAMEAI_CONFIG", "").strip()
        #logger.warning(f"环境变量 WFGAMEAI_CONFIG = {config_from_env if config_from_env else '(未设置)'}")
        # logger.warning(f"环境变量 AI_ENV = {os.environ.get('AI_ENV', '(未设置)')}")
        
        # 项目根目录：utils/config_helper.py -> wfgame-ai-server -> 项目根目录
        project_root = Path(__file__).resolve().parent.parent.parent
        
        # 如果设置了WFGAMEAI_CONFIG环境变量
        if config_from_env:
            # 检查是否为完整路径
            if os.path.isfile(config_from_env):
                # logger.warning(f"使用环境变量指定的配置文件: {config_from_env}")
                return config_from_env
            
            # 检查是否为相对于项目根目录的路径
            full_path = os.path.join(project_root, config_from_env)
            if os.path.isfile(full_path):
                # logger.warning(f"使用环境变量指定的配置文件: {full_path}")
                return full_path
                    
        # 根据 AI_ENV 选择配置文件（仅支持 prod 和 dev）
        ai_env = os.environ.get("AI_ENV", "").strip().lower()
        
        if ai_env == "dev":
            config_file = "config_dev.ini"
        else:
            # prod 环境或未指定时，默认使用 config.ini
            config_file = "config.ini"
        
        final_path = os.path.join(project_root, config_file)
        # logger.warning(f"根据 AI_ENV={ai_env if ai_env else '(未设置)'} 选择配置文件: {final_path}")
        return final_path

    def get_path(self, key, create_if_missing=True):
        """
        获取配置文件中的路径

        Args:
            key: 路径配置项名称 (如 'project_root', 'server_dir' 等)
            create_if_missing: 如果目录不存在是否创建

        Returns:
            str: 标准化的路径字符串
        """
        try:
            path = self._config.get('paths', key)
            path = os.path.normpath(path)

            # 如果是目录且不存在，则创建
            if create_if_missing and key.endswith('_dir') and not os.path.exists(path):
                os.makedirs(path, exist_ok=True)

            return path
        except (configparser.NoSectionError, configparser.NoOptionError) as e:
            # logger.error(f"配置文件中找不到路径: {key}, 错误: {e}")
            return None

    def get(self, section, key, fallback=None):
        """获取一般配置项"""
        return self._config.get(section, key, fallback=fallback)

    def getint(self, section, key, fallback=None):
        """获取整数配置项"""
        return self._config.getint(section, key, fallback=fallback)

    def getboolean(self, section, key, fallback=None):
        """获取布尔配置项"""
        return self._config.getboolean(section, key, fallback=fallback)

    def get_file_path(self, base_dir_key, *rel_paths):
        """
        根据基础目录和相对路径，构建完整的文件路径

        Args:
            base_dir_key: 基础目录的配置键名
            *rel_paths: 相对路径部分

        Returns:
            str: 完整的文件路径
        """
        base_dir = self.get_path(base_dir_key)
        if not base_dir:
            return None
        return os.path.normpath(os.path.join(base_dir, *rel_paths))

# 提供全局单例实例
config = ConfigManager()

def update_best_model(project_root):
    """更新最佳模型文件，从旧版utils.py保留的功能"""
    # 找到最新的实验目录
    train_dir = os.path.join(project_root, "train_results", "train")
    if not os.path.exists(train_dir):
        return {"success": False, "message": "训练目录不存在"}

    exp_dirs = sorted([d for d in os.listdir(train_dir) if os.path.isdir(os.path.join(train_dir, d))])
    if not exp_dirs:
        return {"success": False, "message": "没有找到实验目录"}

    latest_exp = exp_dirs[-1]
    weights_dir = os.path.join(train_dir, latest_exp, "weights")
    if not os.path.exists(weights_dir):
        return {"success": False, "message": f"权重目录不存在: {weights_dir}"}

    # 找到best.pt文件
    best_pt = os.path.join(weights_dir, "best.pt")
    if not os.path.exists(best_pt):
        return {"success": False, "message": f"未找到best.pt文件: {best_pt}"}

    # 复制到models目录
    models_dir = os.path.join(project_root, "models")
    os.makedirs(models_dir, exist_ok=True)
    target_path = os.path.join(models_dir, "best.pt")

    try:
        shutil.copy2(best_pt, target_path)
        return {
            "success": True,
            "message": f"已更新best.pt文件",
            "source": best_pt,
            "target": target_path
        }
    except Exception as e:
        return {
            "success": False,
            "message": f"更新best.pt文件失败: {str(e)}"
        }

# 常用路径访问函数
def get_project_root():
    return config.get_path('project_root')

def get_server_dir():
    return config.get_path('server_dir')

def get_scripts_dir():
    return config.get_path('scripts_dir')

def get_testcase_dir():
    return config.get_path('testcase_dir')

def get_reports_dir():
    return config.get_path('reports_dir')

def get_ui_reports_dir():
    return config.get_path('ui_reports_dir')

def get_datasets_dir():
    return config.get_path('datasets_dir')

def get_weights_dir():
    return config.get_path('weights_dir')

def get_train_results_dir():
    return config.get_path('train_results_dir')

def get_weights_path():
    """获取模型文件路径"""
    weights_dir = get_weights_dir()
    if weights_dir and os.path.exists(weights_dir):
        # 查找最新的best.pt文件
        best_files = sorted(glob.glob(os.path.join(weights_dir, "best*.pt")), key=os.path.getmtime, reverse=True)
        if best_files:
            return best_files[0]

    # 如果在weights_dir中找不到，则尝试在项目根目录的models文件夹中查找
    models_dir = os.path.join(get_project_root(), "models")
    if os.path.exists(models_dir):
        best_pt = os.path.join(models_dir, "best.pt")
        if os.path.exists(best_pt):
            return best_pt

    # 如果都找不到，返回None
    logger.warning("找不到模型文件 best.pt")
    return None

def get_model_path():
    """获取模型文件路径"""
    weights_path = get_weights_path()
    if weights_path:
        return weights_path

    # 如果无法获取权重路径，返回默认模型路径
    project_root = get_project_root()
    if project_root:
        default_model_path = os.path.join(project_root, "models", "best.pt")
        if os.path.exists(default_model_path):
            return default_model_path

    # 如果所有尝试都失败，返回None
    logger.warning("无法找到有效的模型路径")
    return None

@dataclass
class RedisConfigObj:
    host: str
    port: int
    username: Optional[str] = None
    password: Optional[str] = None
    db: int = 0
    redis_url: str = ""
    # 连接池配置
    max_connections: int = 20
    socket_connect_timeout: int = 5
    socket_timeout: int = 5
    retry_on_timeout: bool = True
    health_check_interval: int = 30

def get_redis_conn(config_type="redis") -> RedisConfigObj:
    host = config.get(config_type, 'host', fallback='localhost')
    port = config.getint(config_type, 'port', fallback=6379)
    username = config.get(config_type, 'username', fallback="")
    password = config.get(config_type, 'password', fallback="")
    db = config.getint(config_type, 'db', fallback=0)
    # 连接池配置
    max_connections = config.getint(config_type, 'max_connections', fallback=20)
    socket_connect_timeout = config.getint(config_type, 'socket_connect_timeout', fallback=5)
    socket_timeout = config.getint(config_type, 'socket_timeout', fallback=5)
    retry_on_timeout = config.getboolean(config_type, 'retry_on_timeout', fallback=True)
    health_check_interval = config.getint(config_type, 'health_check_interval', fallback=30)

    # 构建 Redis URL：仅在有用户名和密码时才包含认证信息
    if username and password:
        redis_url = f'redis://{username}:{password}@{host}:{port}/{db}'
    else:
        redis_url = f'redis://{host}:{port}/{db}'
    
    return RedisConfigObj(
        host=host,
        port=port,
        username=username if username else None,
        password=password if password else None,
        db=db,
        redis_url=redis_url,
        max_connections=max_connections,
        socket_connect_timeout=socket_connect_timeout,
        socket_timeout=socket_timeout,
        retry_on_timeout=retry_on_timeout,
        health_check_interval=health_check_interval
    )


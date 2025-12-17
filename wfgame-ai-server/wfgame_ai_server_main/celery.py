"""
Celery配置
"""

from __future__ import absolute_import, unicode_literals
import os
from celery import Celery, Task
from django.conf import settings
import uuid
import threading

# 设置默认Django配置模块
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'wfgame_ai_server_main.settings')

# 创建Celery应用（增强健壮性：统一使用 settings 中的 Redis URL，增加心跳、重试与 keepalive）
redis_url = getattr(settings, 'CELERY_REDIS_CFG', None).redis_url if hasattr(settings, 'CELERY_REDIS_CFG') else os.getenv('CELERY_REDIS_URL', 'redis://localhost:6379/0')
AI_CELERY = Celery(
    'wfgame_ai_server_main',
    backend=redis_url,
    broker=redis_url,
)

# 健壮性配置：断线重连、心跳、socket keepalive、健康检查等
AI_CELERY.conf.update(
    broker_connection_retry_on_startup=True,   # 启动时断连也无限重试
    broker_connection_max_retries=None,        # 运行中连接无限重试
    broker_heartbeat=30,                       # 心跳，防止空闲断线
    broker_pool_limit=None,                    # 避免连接池过小（None 表示不限；也可设置为具体数值）
    broker_transport_options={
        'health_check_interval': 30,          # kombu 对 Redis 的健康检查间隔
        'socket_keepalive': True,             # 启用 TCP keepalive
        'socket_timeout': 30,                 # 读写超时
        'socket_connect_timeout': 30,         # 连接超时
        'retry_on_timeout': True,             # 超时重试
    },
    result_backend=redis_url,
    # 自定义日志格式：简化输出，删除进程信息
    worker_log_format='[%(asctime)s: %(levelname)s] %(message)s',
    worker_task_log_format='[%(asctime)s: %(levelname)s] %(message)s',
)

# 使用字符串表示，以避免将对象本身通过pickle序列化
AI_CELERY.config_from_object('django.conf:settings', namespace='CELERY')

def _has_alive_worker(app: Celery, timeout: float = 2.0) -> bool:
    """检测是否有可用的Celery Worker"""
    import logging
    logger = logging.getLogger(__name__)
    
    try:
        logger.info(f"🔍 检测Celery Worker状态（超时: {timeout}秒）...")
        replies = app.control.ping(timeout=timeout)
        
        if replies:
            worker_names = [list(r.keys())[0] if r else 'unknown' for r in replies]
            logger.info(f"✅ 检测到 {len(replies)} 个Worker: {', '.join(worker_names)}")
            return True
        else:
            logger.warning("⚠️ 未检测到任何可用的Celery Worker")
            logger.warning("💡 请确认Worker已启动，可运行: python start_celery_worker.py")
            return False
    except Exception as e:
        logger.error(f"❌ Worker状态检测异常: {e}")
        import traceback
        logger.debug(traceback.format_exc())
        return False


class GuardTask(Task):
    """投递前探活：无 worker (且非 eager/未跳过) 不投递 -> 返回 None

    调用方式保持: res = task.delay(...)
    判断是否投递: if res is None: 无 worker
    跳过检查: task.apply_async(..., skip_worker_check=True)
    不伪造 AsyncResult，避免生命周期副作用
    """
    abstract = True

    def apply_async(self, args=None, kwargs=None, **options):  # type: ignore[override]
        try:
            eager = bool(getattr(settings, 'CELERY_TASK_ALWAYS_EAGER', False))
            timeout = float(getattr(settings, 'CELERY_WORKER_HEALTHCHECK_TIMEOUT', 2.0))
        except Exception:
            eager, timeout = False, 2.0

        # 统一确保 task_id 存在，便于视图层稳定通过 async_res.id 获取
        if not options.get('task_id'):
            prefix = ''
            try:
                if args and len(args) > 0:
                    a0 = args[0]
                    if isinstance(a0, (int, str)) and str(a0).strip():
                        prefix = f"task-{str(a0).strip()}-"
            except Exception:
                pass
            options['task_id'] = f"{prefix}{uuid.uuid4().hex}"

        skip = bool(options.pop('skip_worker_check', False))
        if eager:
            # 在 eager 模式下，默认采用后台线程执行，避免阻塞请求返回
            eager_async = True
            try:
                eager_async = bool(getattr(settings, 'CELERY_EAGER_ASYNC', True))
            except Exception:
                eager_async = True
            if eager_async:
                task_id = options.get('task_id')
                def _bg_run():
                    try:
                        return self.run(*(args or ()), **(kwargs or {}))
                    except Exception:
                        import traceback as _tb
                        print('[Celery][EagerAsync] 任务执行异常:', _tb.format_exc())
                        return None
                th = threading.Thread(target=_bg_run, name=f"celery-eager-{task_id}", daemon=True)
                th.start()
                class _LightAsyncResult:
                    def __init__(self, tid):
                        self.id = tid
                        self.task_id = tid
                return _LightAsyncResult(task_id)
            # 若显式关闭 CELERY_EAGER_ASYNC，则退回 Celery 的同步执行（将阻塞至任务结束）
            return super().apply_async(args=args, kwargs=kwargs, **options)

        if skip:
            return super().apply_async(args=args, kwargs=kwargs, **options)

        if not _has_alive_worker(self._get_app(), timeout=timeout):
            return None
        return super().apply_async(args=args, kwargs=kwargs, **options)

AI_CELERY.Task = GuardTask
# 自动从所有已注册的Django应用中加载任务
AI_CELERY.autodiscover_tasks()

# 调试提示：若启用 Eager 模式，在启动日志中打印提醒
try:
    if getattr(settings, 'CELERY_TASK_ALWAYS_EAGER', False):
        print('[Celery][Eager] CELERY_TASK_ALWAYS_EAGER=True (任务将同步执行，无需独立 worker)')
except Exception:
    pass

@AI_CELERY.task(bind=True)
def debug_task(self):
    """测试任务，用于调试"""
    print(f'Request: {self.request!r}')
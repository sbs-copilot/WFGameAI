# -*- coding: utf-8 -*-
"""
启动 Celery Worker（支持环境隔离与可选自动重载）

用法示例：
    # 开发环境（使用 config_dev.ini，对应队列 ai_queue，可选自动重载）
    python start_celery_worker.py --env dev --autoreload
    
    # 线上环境（使用 config.ini，对应队列 ai_queue_prod，默认禁用自动重载）
    python start_celery_worker.py --env prod


说明:
- 通过 --env 隔离队列名、worker 名称、pid/log 文件，避免不同环境互相影响
- 默认使用 solo 池，兼容 Windows
- 仅在显式传入 --autoreload 时启用源码监听；生产环境默认关闭
"""
import os
import sys
import argparse
import subprocess
import time
import datetime
from typing import Dict, List


def build_command(python_exec: str, queue: str, name: str,
                  loglevel: str) -> List[str]:
    """构建 Celery 启动命令参数列表。"""
    return [
        python_exec, '-m', 'celery',
        '-A', 'wfgame_ai_server_main',
        'worker',
        '--pool=solo',
        '-l', str(loglevel),
        '-n', str(name),
        '-Q', str(queue),
        '-E',
        '--without-mingle',
        '--without-gossip',
    ]


def snapshot_py_mtimes(root_dir: str) -> Dict[str, float]:
    """获取目录下所有 .py 文件的修改时间快照。"""
    mtimes: Dict[str, float] = {}
    for base, _dirs, files in os.walk(root_dir):
        if any(x in base for x in (
            os.sep + '__pycache__', os.sep + 'media' + os.sep,
            os.sep + 'static' + os.sep, os.sep + 'staticfiles' + os.sep
        )):
            continue
        for fn in files:
            if fn.endswith('.py'):
                p = os.path.join(base, fn)
                try:
                    mtimes[p] = os.path.getmtime(p)
                except Exception:
                    continue
    return mtimes


def has_changes(prev: Dict[str, float], cur: Dict[str, float]) -> bool:
    """比较两次快照，判断是否有变更（新增/删除/修改）。"""
    if prev.keys() != cur.keys():
        return True
    for k, v in cur.items():
        if prev.get(k) != v:
            return True
    return False


def stop_existing_worker(pid_file: str) -> None:
    """停止已存在的Celery Worker进程"""
    if not os.path.exists(pid_file):
        return
    
    try:
        with open(pid_file, 'r', encoding='utf-8') as f:
            pid_str = f.read().strip()
            if not pid_str:
                return
            pid = int(pid_str)
        
        print(f'检测到已存在的Worker进程 (PID: {pid})，正在停止...')
        
        # Windows系统使用taskkill
        if sys.platform == 'win32':
            try:
                subprocess.run(['taskkill', '/F', '/PID', str(pid)], 
                             capture_output=True, timeout=5)
                time.sleep(1)
                print(f'已停止旧的Worker进程 (PID: {pid})')
            except Exception as e:
                print(f'停止进程失败: {e}')
        else:
            # Linux/Mac使用kill
            try:
                import signal
                os.kill(pid, signal.SIGTERM)
                time.sleep(1)
                print(f'已停止旧的Worker进程 (PID: {pid})')
            except ProcessLookupError:
                print(f'进程 {pid} 不存在，可能已停止')
            except Exception as e:
                print(f'停止进程失败: {e}')
        
        # 删除PID文件
        try:
            os.remove(pid_file)
        except Exception:
            pass
            
    except Exception as e:
        print(f'读取PID文件失败: {e}')


def main() -> int:
    """入口函数：启动 Celery Worker，支持环境隔离与可选自动重载。"""
    parser = argparse.ArgumentParser(description='启动 Celery Worker')
    parser.add_argument('--env', type=str, default=os.environ.get('AI_ENV', 'dev'),
                        help='环境标识(dev/prod/test等)，用于隔离队列、worker、pid与日志')
    parser.add_argument('--queue', type=str, default='ai_queue', help='基础队列名（会追加环境后缀）')
    parser.add_argument('--name', type=str, default='ai_worker', help='基础Worker名（会追加环境后缀）')
    parser.add_argument('--loglevel', type=str, default='info', help='日志等级')
    parser.add_argument('--interval', type=float, default=1.5, help='检测间隔(秒)')
    parser.add_argument('--autoreload', action='store_true', help='启用源码自动重载(仅开发环境建议)')
    args = parser.parse_args()

    env_name = (args.env or 'dev').strip()
    env_suffix = f"_{env_name}"

    # 队列/worker 名称附加环境后缀，避免冲突
    # queue_name = f"{args.queue}{env_suffix}"
    # 按环境设置队列名称
    queue_name = f"ai_queue{'_prod' if env_name == 'prod' else ''}"
    worker_name = f"{args.name}{env_suffix}"

    project_root = os.path.abspath(os.path.dirname(__file__))
    celery_cwd = os.path.join(project_root, 'wfgame-ai-server')

    if not os.path.isdir(celery_cwd):
        print('错误: 未找到 wfgame-ai-server 目录，请在项目根目录运行本脚本。')
        return 1

    python_exec = sys.executable
    base_cmd = build_command(python_exec, queue_name, worker_name, args.loglevel)

    # 环境变量：确保实时输出与更友好的日志
    env = dict(os.environ)
    env['PYTHONUNBUFFERED'] = '1'
    # 关键：将当前环境写入子进程，驱动 ConfigManager 选择 config_{env}.ini
    env['AI_ENV'] = env_name
    
    # 明确指定配置文件路径，避免路径查找错误
    # prod环境使用config.ini，其他环境使用config_{env}.ini
    if env_name == 'prod':
        config_file = os.path.join(project_root, 'config.ini')
    else:
        config_file = os.path.join(project_root, f'config_{env_name}.ini')
    
    env['WFGAMEAI_CONFIG'] = config_file

    # 隔离 pid/log 文件，避免多环境冲突
    run_dir = os.path.join(project_root, 'run')
    try:
        os.makedirs(run_dir, exist_ok=True)
    except Exception:
        pass
    pid_file = os.path.join(run_dir, f'celery{env_suffix}.pid')
    log_file = os.path.join(run_dir, f'celery{env_suffix}.log')

    # 停止已存在的Worker进程
    stop_existing_worker(pid_file)

    # 清空日志文件（重启时重新开始记录）
    try:
        with open(log_file, 'w', encoding='utf-8') as f:
            f.write(f"=== Celery Worker 启动于 {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===\n")
        print(f'已清空日志文件: {log_file}')
    except Exception as e:
        print(f'清空日志文件失败: {e}')

    # 将日志重定向到文件（保留控制台输出）
    log_fp = open(log_file, 'a', encoding='utf-8', errors='ignore')

    # 仅在开发环境且显式指定 --autoreload 时启用自动重载
    enable_reload = (env_name != 'prod') and bool(args.autoreload)

    watch_dir = celery_cwd
    print('启动 Celery Worker...')
    print('环境:', env_name)
    print('工作目录:', celery_cwd)
    print('监控目录:', watch_dir if enable_reload else '(未启用自动重载)')
    print('命令:', ' '.join(base_cmd))
    print('PID文件:', pid_file)
    print('日志文件:', log_file)

    proc = None
    try:
        prev_snapshot = snapshot_py_mtimes(watch_dir) if enable_reload else {}
        # 首次启动
        proc = subprocess.Popen(base_cmd, cwd=celery_cwd, env=env, stdout=log_fp, stderr=log_fp)
        # 记录 pid
        try:
            with open(pid_file, 'w', encoding='utf-8') as pf:
                pf.write(str(proc.pid))
        except Exception:
            pass

        if not enable_reload:
            # 不启用自动重载则持续等待
            while True:
                time.sleep(2.0)
                if proc.poll() is not None:
                    code = proc.returncode
                    print('Worker 已退出，返回码:', code)
                    return code
        else:
            # 自动重载模式
            while True:
                time.sleep(max(0.2, float(args.interval)))
                if proc.poll() is not None:
                    code = proc.returncode
                    print('Worker 已退出，返回码:', code)
                    return code
                cur_snapshot = snapshot_py_mtimes(watch_dir)
                if has_changes(prev_snapshot, cur_snapshot):
                    print('检测到源码变更，正在重启 Worker ...')
                    try:
                        proc.terminate()
                        try:
                            proc.wait(timeout=10)
                        except Exception:
                            proc.kill()
                    except Exception:
                        pass
                    
                    # 关闭旧的日志文件句柄
                    try:
                        log_fp.close()
                    except Exception:
                        pass
                    
                    # 清空日志文件并重新打开
                    try:
                        with open(log_file, 'w', encoding='utf-8') as f:
                            f.write(f"=== Celery Worker 自动重启于 {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===\n")
                    except Exception:
                        pass
                    log_fp = open(log_file, 'a', encoding='utf-8', errors='ignore')
                    
                    # 重启
                    prev_snapshot = cur_snapshot
                    proc = subprocess.Popen(base_cmd, cwd=celery_cwd, env=env, stdout=log_fp, stderr=log_fp)
                    try:
                        with open(pid_file, 'w', encoding='utf-8') as pf:
                            pf.write(str(proc.pid))
                    except Exception:
                        pass
                    print(f'✅ Worker已重启 (新PID: {proc.pid})')
    except KeyboardInterrupt:
        print('\n已中断。正在退出...')
        try:
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except Exception:
                    proc.kill()
        except Exception:
            pass
        return 130
    except Exception as e:
        print('启动失败:', e)
        try:
            if proc and proc.poll() is None:
                proc.terminate()
        except Exception:
            pass
        return 1
    finally:
        try:
            if log_fp:
                log_fp.flush()
                # 不在这里关闭文件句柄，交由系统在进程结束时回收
        except Exception:
            pass


if __name__ == '__main__':
    sys.exit(main()) 
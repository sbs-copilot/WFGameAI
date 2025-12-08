#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WFGame AI 统一服务启动脚本
同时启动 Django 后端服务和 Celery Worker，支持代码修改自动重载

用法示例：
    # 开发环境（自动重载）
    python start_all_services.py --env dev
    
    # 生产环境（禁用自动重载）
    python start_all_services.py --env prod

功能特性：
- 一键启动/停止所有服务
- 支持代码修改自动重载（开发环境）
- 统一的日志输出和错误处理
- Ctrl+C 优雅退出所有服务
"""
import os
import sys
import time
import subprocess
import threading
import signal
import argparse
import psutil
from typing import Dict, List, Optional
from datetime import datetime

# 修复 Windows 控制台编码问题
if sys.platform == 'win32':
    import locale
    # 尝试设置控制台为 UTF-8
    try:
        if sys.stdout.encoding != 'utf-8':
            import io
            sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
            sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
    except Exception:
        pass

# 全局变量
processes = []
should_exit = False


def build_subprocess_env(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """构造子进程环境变量，确保使用UTF-8编码"""
    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    if sys.platform == "win32":
        env.setdefault("LANG", "zh_CN.UTF-8")
        env.setdefault("LC_ALL", "zh_CN.UTF-8")
    if extra:
        env.update(extra)
    return env


def print_colored(text, color='white'):
    """打印彩色文本"""
    colors = {
        'red': '\033[91m',
        'green': '\033[92m',
        'yellow': '\033[93m',
        'blue': '\033[94m',
        'magenta': '\033[95m',
        'cyan': '\033[96m',
        'white': '\033[97m',
        'reset': '\033[0m'
    }
    # 修复 Windows 控制台 Unicode 编码问题
    try:
        print(f"{colors.get(color, colors['white'])}{text}{colors['reset']}")
    except UnicodeEncodeError:
        # 如果遇到编码错误，移除 emoji 后重试
        import re
        text_no_emoji = re.sub(r'[^\u0000-\uFFFF]', '', text)
        print(f"{colors.get(color, colors['white'])}{text_no_emoji}{colors['reset']}")


def get_env_config(env: str) -> Dict:
    """获取环境配置"""
    if env == 'dev':
        return {
            'config_file': 'config_dev.ini',
            'celery_queue': 'ai_queue',
            'celery_worker_name': 'ai_worker_dev',
            'celery_pid_file': 'run/celery_dev.pid',
            'celery_log_file': 'run/celery_dev.log',
            'autoreload': False,
            'backend_port': 9000
        }
    else:  # prod
        return {
            'config_file': 'config.ini',
            'celery_queue': 'ai_queue_prod',
            'celery_worker_name': 'ai_worker_prod',
            'celery_pid_file': 'run/celery_prod.pid',
            'celery_log_file': 'run/celery_prod.log',
            'autoreload': False,
            'backend_port': 8000
        }


def stop_process_by_pid_file(pid_file: str, service_name: str) -> None:
    """通过PID文件停止进程"""
    if not os.path.exists(pid_file):
        return
    
    try:
        with open(pid_file, 'r', encoding='utf-8') as f:
            pid = int(f.read().strip())
        
        if psutil.pid_exists(pid):
            print_colored(f"[{service_name}] 检测到已存在的进程 (PID: {pid})，正在停止...", 'yellow')
            try:
                process = psutil.Process(pid)
                process.terminate()
                process.wait(timeout=5)
                print_colored(f"[{service_name}] 已停止旧进程 (PID: {pid})", 'green')
            except psutil.TimeoutExpired:
                process.kill()
                print_colored(f"[{service_name}] 强制终止进程 (PID: {pid})", 'yellow')
            except Exception as e:
                print_colored(f"[{service_name}] 停止进程失败: {e}", 'red')
        
        os.remove(pid_file)
    except Exception as e:
        print_colored(f"[{service_name}] 读取PID文件失败: {e}", 'red')


def clear_log_file(log_file: str) -> None:
    """清空日志文件"""
    try:
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
        with open(log_file, 'w', encoding='utf-8') as f:
            f.write(f"=== 服务启动于 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===\n")
        print_colored(f"已清空日志文件: {log_file}", 'cyan')
    except Exception as e:
        print_colored(f"清空日志文件失败: {e}", 'red')


def start_celery_worker(config: Dict, python_exec: str) -> Optional[subprocess.Popen]:
    """启动 Celery Worker - 使用 start_celery_worker.py"""
    print_colored("\n" + "="*60, 'cyan')
    print_colored("启动 Celery Worker...", 'cyan')
    print_colored("="*60, 'cyan')
    
    # 确定环境参数
    env = 'dev' if 'dev' in config['config_file'] else 'prod'
    
    # 使用 start_celery_worker.py 启动（它已经实现了完整的功能）
    cmd = [python_exec, 'start_celery_worker.py', '--env', env]
    
    # 如果启用自动重载，添加参数
    if config.get('autoreload', False):
        cmd.append('--autoreload')
    
    print_colored(f"环境: {env}", 'yellow')
    print_colored(f"自动重载: {'启用' if config.get('autoreload') else '禁用'}", 'yellow')
    print_colored(f"日志文件: {config['celery_log_file']}", 'yellow')
    
    try:
        # 启动进程
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding='utf-8',
            errors='replace',
            bufsize=1,
            env=build_subprocess_env()
        )
        
        print_colored(f"✅ Celery Worker 已启动 (PID: {process.pid})", 'green')
        return process
        
    except Exception as e:
        print_colored(f"❌ Celery Worker 启动失败: {e}", 'red')
        return None


def start_django_backend(config: Dict, python_exec: str) -> Optional[subprocess.Popen]:
    """启动 Django 后端服务"""
    print_colored("\n" + "="*60, 'cyan')
    print_colored("启动 Django 后端服务...", 'cyan')
    print_colored("="*60, 'cyan')
    
    # 构建启动命令
    cmd = [python_exec, 'start_wfgame_ai.py', '--config', config['config_file']]
    
    print_colored(f"配置文件: {config['config_file']}", 'yellow')
    print_colored(f"端口: {config['backend_port']}", 'yellow')
    
    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding='utf-8',
            errors='replace',
            bufsize=1,
            env=build_subprocess_env()
        )
        
        print_colored(f"✅ Django 后端已启动 (PID: {process.pid})", 'green')
        return process
        
    except Exception as e:
        print_colored(f"❌ Django 后端启动失败: {e}", 'red')
        return None


def start_usb_monitor(config: Dict):
    """
    启动USB设备监控脚本

    Returns:
        subprocess.Popen: 监控进程对象
    """
    print_colored("\n====== 启动USB设备监控 ======", 'yellow')

    # 更新路径到 apps/scripts
    monitor_script = os.path.join(
        get_project_root(), 
        "wfgame-ai-server", 
        "apps", 
        "scripts", 
        "monitor_usb.py"
    )

    if not os.path.exists(monitor_script):
        print_colored(f"错误: USB监控脚本不存在: {monitor_script}", 'red')
        return None

    # 配置文件环境变量
    env_vars = {}
    if config['config_path']:
        env_vars['WFGAMEAI_CONFIG'] = config['config_path']

    # 启动监控进程
    process = subprocess.Popen(
        [sys.executable, monitor_script],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding='utf-8',
        errors='replace',
        bufsize=1,
        env=build_subprocess_env(env_vars)
    )

    print_colored(f"✅ USB设备监控已启动 (PID: {process.pid})", 'green')
    return process


def get_project_root() -> str:
    """获取项目根目录"""
    # 当前文件所在目录
    current_dir = os.path.dirname(os.path.abspath(__file__))
    # 项目根目录是当前目录的父目录
    return os.path.abspath(os.path.join(current_dir, ".."))


def monitor_process_output(process: subprocess.Popen, service_name: str, log_file: Optional[str] = None):
    """监控进程输出"""
    global should_exit
    
    try:
        log_handle = None
        if log_file:
            log_handle = open(log_file, 'a', encoding='utf-8', errors='replace')
        
        for line in process.stdout:
            if should_exit:
                break
            
            # 输出到控制台
            print(f"[{service_name}] {line.rstrip()}")
            
            # 写入日志文件
            if log_handle:
                log_handle.write(line)
                log_handle.flush()
        
        if log_handle:
            log_handle.close()
            
    except Exception as e:
        print_colored(f"[{service_name}] 输出监控异常: {e}", 'red')


def snapshot_py_mtimes(root_dir: str) -> Dict[str, float]:
    """获取目录下所有 .py 文件的修改时间快照"""
    mtimes: Dict[str, float] = {}
    for base, _dirs, files in os.walk(root_dir):
        if any(x in base for x in (
            os.sep + '__pycache__', os.sep + 'media' + os.sep,
            os.sep + 'static' + os.sep, os.sep + 'staticfiles' + os.sep,
            os.sep + 'node_modules' + os.sep
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
    """比较两次快照，判断是否有变更"""
    if prev.keys() != cur.keys():
        return True
    for k, v in cur.items():
        if prev.get(k) != v:
            return True
    return False


def auto_reload_monitor(config: Dict, python_exec: str):
    """自动重载监控线程 - 仅监控 Django 后端"""
    global should_exit, processes
    
    if not config['autoreload']:
        return
    
    print_colored("\n🔄 Django 后端自动重载已启用", 'green')
    print_colored("   (Celery Worker 有独立的自动重载机制)", 'cyan')
    
    # 注意：start_celery_worker.py 已经有自己的自动重载功能
    # 这里只需要监控 Django 后端即可
    # 实际上 Django runserver 本身也有自动重载，所以这个监控可以简化或移除


def signal_handler(signum, frame):
    """信号处理器"""
    global should_exit
    print_colored("\n\n接收到终止信号，正在关闭服务...", 'yellow')
    should_exit = True


def cleanup_services(config: Dict):
    """清理所有服务"""
    global processes
    
    print_colored("\n正在关闭服务...", 'yellow')
    
    # 停止所有进程
    for proc in processes[:]:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        except Exception as e:
            print_colored(f"停止进程失败: {e}", 'red')
    
    # 清理PID文件
    if os.path.exists(config['celery_pid_file']):
        try:
            os.remove(config['celery_pid_file'])
        except Exception:
            pass
    
    print_colored("所有服务已关闭", 'green')


def main():
    """主函数"""
    global processes, should_exit
    
    # 解析命令行参数
    parser = argparse.ArgumentParser(description='WFGame AI 统一服务启动脚本')
    parser.add_argument('--env', choices=['dev', 'prod'], default='dev',
                        help='运行环境 (dev=开发环境, prod=生产环境)')
    args = parser.parse_args()
    
    # 获取配置
    config = get_env_config(args.env)
    python_exec = sys.executable
    
    # 注册信号处理器
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # 打印启动信息
    print_colored("\n" + "="*60, 'cyan')
    print_colored("WFGame AI 服务启动器", 'cyan')
    print_colored("="*60, 'cyan')
    print_colored(f"环境: {args.env.upper()}", 'yellow')
    print_colored(f"Python: {python_exec}", 'yellow')
    print_colored(f"自动重载: {'启用' if config['autoreload'] else '禁用'}", 'yellow')
    print_colored("="*60 + "\n", 'cyan')
    
    try:
        # 启动 Celery Worker
        celery_proc = start_celery_worker(config, python_exec)
        if celery_proc:
            processes.append(celery_proc)
            threading.Thread(
                target=monitor_process_output,
                args=(celery_proc, 'Celery Worker', config['celery_log_file']),
                daemon=True
            ).start()
        
        # 等待 Celery 启动
        time.sleep(3)
        
        # 启动 Django 后端
        django_proc = start_django_backend(config, python_exec)
        if django_proc:
            processes.append(django_proc)
            threading.Thread(
                target=monitor_process_output,
                args=(django_proc, '后端', None),
                daemon=True
            ).start()
        
        # 启动 USB 监控
        usb_proc = start_usb_monitor(config)
        if usb_proc:
            processes.append(usb_proc)
            threading.Thread(
                target=monitor_process_output,
                args=(usb_proc, 'USB 监控', None),
                daemon=True
            ).start()
        
        # 打印成功信息
        print_colored("\n" + "="*60, 'green')
        print_colored("✅ 所有服务已成功启动！", 'green')
        print_colored("="*60, 'green')
        print_colored(f"后端地址: http://localhost:{config['backend_port']}", 'cyan')
        print_colored(f"Celery日志: {config['celery_log_file']}", 'cyan')
        if config['autoreload']:
            print_colored("\n📝 自动重载状态:", 'cyan')
            print_colored("  - Django 后端: ✅ 内置自动重载", 'green')
            print_colored("  - Celery Worker: ✅ 独立自动重载", 'green')
        print_colored("\n按 Ctrl+C 停止所有服务", 'yellow')
        print_colored("="*60 + "\n", 'green')
        
        # 主循环
        while not should_exit:
            time.sleep(1)
            
            # 检查进程是否异常退出
            for proc in processes[:]:
                if proc.poll() is not None:
                    print_colored(f"\n⚠️ 检测到服务异常退出 (PID: {proc.pid}, 退出码: {proc.returncode})", 'red')
                    processes.remove(proc)
        
    except KeyboardInterrupt:
        print_colored("\n\n接收到中断信号", 'yellow')
    except Exception as e:
        print_colored(f"\n❌ 发生错误: {e}", 'red')
        import traceback
        traceback.print_exc()
    finally:
        cleanup_services(config)


if __name__ == '__main__':
    main()

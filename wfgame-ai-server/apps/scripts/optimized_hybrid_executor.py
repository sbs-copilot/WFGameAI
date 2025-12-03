# -*- coding: utf-8 -*-
"""
智能混合执行器 - 智能混合执行策略
严格按照WFGameAI多设备并发执行优化方案实现
根据设备数量自动选择最优执行方式
"""

import os
import sys
import time
import psutil
from typing import List, Dict, Optional
# ---- Global primary device fallback (for environments without Redis in threads) ----
PRIMARY_DEVICE_MAP: Dict[int, str] = {}
PRIMARY_DEVICE_LOCK = None
try:
    from threading import Lock as _Lock
    PRIMARY_DEVICE_LOCK = _Lock()
except Exception:
    PRIMARY_DEVICE_LOCK = None
from datetime import datetime
from multiprocessing import Process, Manager
from collections import deque
import concurrent.futures
import multiprocessing

# 导入自定义组件
from mysql_account_manager import get_mysql_account_manager, SystemResourceStatus
from adaptive_threshold_manager import get_adaptive_threshold_manager


def _device_worker_with_account(device_serial: str, scripts: List[dict],
                               account: Dict, shared_results: Dict = None, task_id: Optional[int] = None) -> Dict:
    """
    设备工作进程（带账号分配）- 独立函数避免序列化问题

    Args:
        device_serial: 设备序列号
        scripts: 脚本列表
        account: 分配的账号信息
        shared_results: 共享结果字典

    Returns:
        Dict: 执行结果
    """
    try:
        # 在子进程中重新导入必要模块
        import os
        import sys
        from datetime import datetime
        from multiprocessing import current_process

        # 🔧 新增：在子进程中禁用DEBUG日志
        import logging
        logging.getLogger('airtest').setLevel(logging.WARNING)
        logging.getLogger('airtest.core.android.adb').setLevel(logging.WARNING)
        logging.getLogger('adbutils').setLevel(logging.WARNING)

        # 添加当前目录到路径
        current_dir = os.path.dirname(os.path.abspath(__file__))
        if current_dir not in sys.path:
            sys.path.insert(0, current_dir)

        # 添加项目根目录到 Python 路径（关键：解决 utils 包导入问题）
        project_root = os.path.dirname(os.path.dirname(current_dir))
        if project_root not in sys.path:
            sys.path.insert(0, project_root)

        # 导入设备相关模块
        from adbutils import adb

        timestamp = datetime.now().strftime("%H:%M:%S")
        process_id = current_process().pid
        print(f"[Worker-{process_id}][{timestamp}] 设备 {device_serial} 开始执行，账号: {account.get('username', 'N/A')}")

        # 获取设备连接
        device = None
        for dev in adb.device_list():
            if dev.serial == device_serial:
                device = dev
                break

        if not device:
            error_msg = f"设备 {device_serial} 连接失败"
            print(f"[Worker-{process_id}][{timestamp}] ❌ {error_msg}")
            if shared_results is not None:
                shared_results[device_serial] = {
                    'success': False,
                    'error': error_msg,
                    'device_report_dir': None
                }
            return {'success': False, 'error': error_msg}

        # 尝试导入ActionProcessor（延后实例化到日志路径就绪之后）
        try:
            from action_processor import ActionProcessor
        except ImportError as e:
            error_msg = f"ActionProcessor导入失败: {e}"
            print(f"[Worker-{process_id}][{timestamp}] ❌ {error_msg}")
            if shared_results is not None:
                shared_results[device_serial] = {
                    'success': False,
                    'error': error_msg,
                    'device_report_dir': None
                }
            return {'success': False, 'error': error_msg}

        # 尝试导入AI检测功能
        detect_buttons_func = None
        try:
            from replay_script import detect_buttons, load_yolo_model_for_detection
            # 初始化YOLO模型
            if load_yolo_model_for_detection():
                detect_buttons_func = detect_buttons
                print(f"[Worker-{process_id}][{timestamp}] 设备 {device_serial} AI检测功能已加载")
            else:
                print(f"[Worker-{process_id}][{timestamp}] 设备 {device_serial} YOLO模型加载失败")
        except ImportError as e:
            print(f"[Worker-{process_id}][{timestamp}] 设备 {device_serial} AI检测功能导入失败: {e}")
        except Exception as e:
            print(f"[Worker-{process_id}][{timestamp}] 设备 {device_serial} AI检测功能初始化异常: {e}")

        # 创建ActionProcessor实例（稍后在确定日志目录后设置 log_txt_path）
        action_processor = None

        # 执行脚本
        total_success = 0
        total_failed = 0
        executed_scripts = []

        # 辅助：按步骤顺序执行一个“DB脚本（通过ID加载）”
        def _run_db_script(script_cfg: dict, tracker=None, is_last: bool = False) -> bool:
            try:
                from replay_script import load_script_content, StepTracker
                steps, meta, name = load_script_content(script_cfg)
            except Exception as _lse:
                print(f"[Worker-{process_id}][{timestamp}] 设备 {device_serial} 加载脚本失败: {script_cfg} -> {_lse}")
                return False
            print(f"[Worker-{process_id}][{timestamp}] 设备 {device_serial} 执行脚本(ID: {script_cfg.get('script_id')}, 名称: {name}) 共 {len(steps)} 步")
            script_ok = True
            if tracker is not None and task_id is not None:
                try:
                    tracker.start_script(meta={
                        "id": script_cfg.get("script_id"),
                        "name": name,
                        "loop-count": script_cfg.get("loop_count", 1),
                        "loop-index": 1,
                        "max-duration": script_cfg.get("max_duration")
                    }, steps=steps)
                except Exception as _trk_e:
                    print(f"[Worker-{process_id}][{timestamp}] 设备 {device_serial} StepTracker start_script 失败: {_trk_e}")
            for idx, step in enumerate(steps):
                try:
                    if tracker:
                        try:
                            tracker.step_started(idx + 1)
                        except Exception:
                            pass
                    # 优先调用内部方法获得更丰富的 ActionResult
                    raw = None
                    try:
                        raw = action_processor._process_action(step, idx, replay_root or device_serial)
                    except Exception:
                        raw = action_processor.process_action(step, idx, replay_root or device_serial)
                    ok = False
                    if raw is None:
                        ok = False
                    elif hasattr(raw, 'success'):
                        ok = bool(getattr(raw, 'success', False))
                    elif isinstance(raw, (list, tuple)):
                        try:
                            ok = bool(raw[0])
                        except Exception:
                            ok = False
                    else:
                        ok = bool(raw)
                    if not ok:
                        script_ok = False
                    if tracker:
                        try:
                            # 提取截图路径（若存在）
                            local_pic = None
                            error_msg = "执行失败"
                            step_msg = ""
                            
                            if raw is not None:
                                if hasattr(raw, 'screenshot_path'):
                                    local_pic = str(raw.screenshot_path)
                                if hasattr(raw, 'message'):
                                    step_msg = raw.message
                                    if not ok:
                                        error_msg = raw.message
                                # 尝试从 details 获取更详细的错误
                                if hasattr(raw, 'details') and isinstance(raw.details, dict):
                                    if raw.details.get('error'):
                                        error_msg = str(raw.details.get('error'))
                            
                            tracker.step_finished(idx + 1,
                                                   success=ok,
                                                   message=step_msg,
                                                   error_message="" if ok else error_msg,
                                                   local_pic_pth=local_pic)
                        except Exception:
                            pass
                except Exception as _se:
                    print(f"[Worker-{process_id}][{timestamp}] 设备 {device_serial} 步骤 {idx+1} 执行异常: {_se}")
                    script_ok = False
                    if tracker:
                        try:
                            tracker.step_finished(idx + 1,
                                                   success=False,
                                                   message="步骤异常",
                                                   error_message=str(_se))
                        except Exception:
                            pass
            if tracker:
                try:
                    tracker.finish_script(final=is_last)
                except Exception:
                    pass
            return script_ok

        # 准备设备报告目录（供截图与 StepTracker 构建远端路径）
        device_report_dir = None
        try:
            # 与主流程一致的报告目录结构：task_<id>/<serial>_<ts>
            from datetime import datetime as _dt
            _run_ts = _dt.now().strftime('%Y%m%d_%H%M%S')
            if task_id is not None:
                server_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
                device_report_dir = os.path.join(server_dir, 'apps', 'reports', 'tmp', 'replay', f'task_{int(task_id)}', f"{device_serial}_{_run_ts}")
                os.makedirs(device_report_dir, exist_ok=True)
            else:
                from replay_script import REPORT_MANAGER
                if REPORT_MANAGER:
                    device_report_dir = REPORT_MANAGER.create_device_report_dir(device_serial, timestamp=_run_ts)
        except Exception:
            device_report_dir = None
        if not device_report_dir:
            try:
                base_tmp = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'reports', 'tmp', 'hybrid'))
                os.makedirs(base_tmp, exist_ok=True)
                from datetime import datetime as _dt
                _run_ts = _dt.now().strftime('%Y%m%d_%H%M%S')
                device_report_dir = os.path.join(base_tmp, f'{device_serial}_{_run_ts}')
                os.makedirs(device_report_dir, exist_ok=True)
            except Exception:
                device_report_dir = None
        # Fallback 主设备选择（无 Redis 情况下仅设定一次）
        if task_id is not None and PRIMARY_DEVICE_LOCK:
            try:
                from replay_script import print_realtime as _prt
                with PRIMARY_DEVICE_LOCK:
                    if task_id not in PRIMARY_DEVICE_MAP:
                        PRIMARY_DEVICE_MAP[task_id] = device_serial
                        _prt(f"🌟 [GLOBAL] 设定主设备(无Redis): {device_serial}")
            except Exception:
                pass

        # 为设备准备日志与报告目录，并实例化 ActionProcessor
        log_txt_path = None
        try:
            if replay_root:
                os.makedirs(replay_root, exist_ok=True)
                log_txt_path = os.path.join(replay_root, 'log.txt')
                # 确保存在空的 log.txt
                with open(log_txt_path, 'a', encoding='utf-8') as _f:
                    pass
        except Exception:
            log_txt_path = None

        action_processor = ActionProcessor(
            device=device,
            device_name=device_serial,
            log_txt_path=log_txt_path,
            detect_buttons_func=detect_buttons_func
        )

        # 不在此处推送在线/离线事件；由 replay_script 前置校验统一推送

        # 设置账号信息
        if account and hasattr(action_processor, 'set_device_account'):
            action_processor.set_device_account(account)

        # 正式遍历脚本列表并执行
        # 统一的 StepTracker（跨脚本保留历史）
        shared_tracker = None
        replay_root = None
        if task_id is not None:
            try:
                # 期望目录: ${server_dir}/apps/reports/tmp/replay/task_<id>/<device_serial>_<ts>
                server_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
                from datetime import datetime as _dt
                _run_ts = _dt.now().strftime('%Y%m%d_%H%M%S')
                replay_root = os.path.join(server_dir, 'apps', 'reports', 'tmp', 'replay', f'task_{int(task_id)}', f"{device_serial}_{_run_ts}")
                os.makedirs(replay_root, exist_ok=True)
                from replay_script import StepTracker
                shared_tracker = StepTracker(task_id=int(task_id), device_serial=device_serial, device_report_dir=replay_root)
                # 对齐 StepTracker 的远端对象目录，确保与本地目录同名（含时间戳）
                try:
                    shared_tracker._run_dir_name = f"{device_serial}_{_run_ts}"
                    _task_part = f"task_{shared_tracker.task_id}" if getattr(shared_tracker, 'task_id', None) else "session"
                    shared_tracker._object_root = f"replay_tasks/{_task_part}/{shared_tracker._run_dir_name}".replace('//','/')
                    _scheme = getattr(shared_tracker, '_scheme', 'http')
                    _host = getattr(shared_tracker, '_host', 'localhost')
                    _bucket = getattr(shared_tracker, '_bucket', 'wfgame-ai')
                    shared_tracker._url_base = f"{_scheme}://{_host}/{_bucket}/{shared_tracker._object_root}/"
                except Exception:
                    pass
                # 用于返回值的设备报告目录与动作处理保存
                device_report_dir = replay_root
            except Exception as _st_e:
                print(f"[Worker-{process_id}][{timestamp}] 设备 {device_serial} 初始化共享 StepTracker 失败: {_st_e}")

        for idx_script, script_config in enumerate(scripts):
            # 1) DB脚本: dict 且包含 script_id
            if isinstance(script_config, dict) and script_config.get('script_id'):
                try:
                    ok = _run_db_script(script_config, tracker=shared_tracker, is_last=(idx_script == len(scripts) - 1))
                    total_success += 1 if ok else 0
                    total_failed += 0 if ok else 1
                    executed_scripts.append({
                        'script': f"id:{script_config.get('script_id')}",
                        'success': 1 if ok else 0,
                        'failed': 0 if ok else 1
                    })
                except Exception as _e1:
                    total_failed += 1
                    print(f"[Worker-{process_id}][{timestamp}] 设备 {device_serial} 脚本执行异常(id:{script_config.get('script_id')}): {_e1}")
                continue

            # 2) 兼容旧的文件脚本路径（可能已经弃用）
            script_path = None
            if isinstance(script_config, dict):
                script_path = script_config.get('path')
            else:
                script_path = script_config

            if not script_path:
                # 无法识别的脚本配置，跳过
                continue

                try:
                    base = os.path.basename(script_path)
                    print(f"[Worker-{process_id}][{timestamp}] 设备 {device_serial} 执行脚本: {base}")
                    result = action_processor.process_script(script_path)
                    success = bool(getattr(result, 'success', result))
                    total_success += 1 if success else 0
                    total_failed += 0 if success else 1
                    executed_scripts.append({
                        'script': base,
                        'success': 1 if success else 0,
                        'failed': 0 if success else 1
                    })
                    print(f"[Worker-{process_id}][{timestamp}] 设备 {device_serial} 脚本 {base} {'成功' if success else '失败'}")
                except Exception as e:
                    total_failed += 1
                    print(f"[Worker-{process_id}][{timestamp}] 设备 {device_serial} 脚本执行异常: {e}")
                # 非 DB 脚本暂不进入 StepTracker 聚合

        # 准备返回结果
        final_result = {
            'success': total_failed == 0,
            'total_success': total_success,
            'total_failed': total_failed,
            'executed_scripts': executed_scripts,
            'device_report_dir': device_report_dir
        }

        # 批量上传并填充远端地址，同时写入设备结果JSON（与单设备路径保持一致）
        try:
            if shared_tracker is not None:
                shared_tracker._batch_upload_and_fill()
        except Exception:
            pass
        try:
            if task_id is not None:
                from replay_script import write_result as _write_result
                # 结果根目录: ${server_dir}/apps/reports/tmp/replay/task_<id>
                server_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
                log_root = os.path.join(server_dir, 'apps', 'reports', 'tmp', 'replay', f'task_{int(task_id)}')
                os.makedirs(log_root, exist_ok=True)
                # 映射为已完成的业务状态（是否所有脚本都成功）
                result_payload = {
                    "exit_code": 0 if final_result.get('success') else -1,
                    "report_url": str(device_report_dir) if device_report_dir else "",
                    "message": "脚本执行完成" if final_result.get('success') else "脚本执行失败",
                }
                _write_result(log_root, device_serial, result_payload)
        except Exception:
            pass

        if shared_results is not None:
            shared_results[device_serial] = final_result

        print(f"[Worker-{process_id}][{timestamp}] 设备 {device_serial} 执行完成，成功:{total_success}，失败:{total_failed}")
        return final_result

    except Exception as e:
        error_msg = f"设备 {device_serial} 执行异常: {e}"
        print(f"❌ {error_msg}")
        import traceback
        traceback.print_exc()

        final_result = {
            'success': False,
            'error': error_msg,
            'device_report_dir': None
        }

        if shared_results is not None:
            shared_results[device_serial] = final_result

        return final_result


class OptimizedHybridExecutor:
    """优化的混合执行器 - 智能混合执行策略"""

    def __init__(self):
        """初始化智能混合执行器"""
        self.threshold_manager = get_adaptive_threshold_manager()
        self.account_manager = get_mysql_account_manager()

    def execute_multi_device(self, device_serials: List[str], scripts: List[dict], task_id: Optional[int] = None) -> Dict:
        """
        核心执行入口

        Args:
            device_serials: 设备序列号列表
            scripts: 脚本列表

        Returns:
            Dict: 执行结果
        """
        device_count = len(device_serials)
        optimal_threshold = self.threshold_manager.get_optimal_threshold()

        print(f"📱 设备数量: {device_count}")
        print(f"🎯 当前最优阈值: {optimal_threshold}")

        # 预分配账号 - 解决账号冲突
        print(f"🔐 预分配账号中...")
        account_allocations = self.account_manager.allocate_account_batch(device_serials)
        # 全或无：只要有设备未分配到账号，则视为失败
        if not account_allocations or len(account_allocations) != len(device_serials):
            missing = [d for d in device_serials if d not in (account_allocations or {})]
            if missing:
                print(f"❌ 账号分配失败，以下设备未分配到账号: {', '.join(missing)}")
            else:
                print(f"❌ 账号分配失败，无法执行")
            return {
                'success': False,
                'error': '账号分配失败',
                'strategy': 'failed'
            }

        try:
            start_time = time.time()

            # 使用原始设备列表执行（全或无已保证全部分配）
            device_count = len(device_serials)
            if device_count <= optimal_threshold:
                # 无限制并发执行
                print(f"🚀 执行策略：无限制并发执行")
                result = self._unlimited_execution(device_serials, scripts, account_allocations, task_id=task_id)
            else:
                # 智能动态管理
                print(f"⚙️ 执行策略：智能动态管理")
                result = self._intelligent_execution(device_serials, scripts, account_allocations, task_id=task_id)

            execution_time = time.time() - start_time

            # 记录性能数据用于自适应调整
            self.threshold_manager.record_performance(device_count, execution_time)

            result['execution_time'] = execution_time
            return result

        finally:
            # 释放账号资源
            print(f"🔓 释放账号资源...")
            self.account_manager.release_account_batch(device_serials)

    def execute_multi_device_enhanced(self, device_serials: List[str], scripts: List[str]) -> Dict:
        """
        增强版多设备执行入口 - 包含完整的监控和调整

        Args:
            device_serials: 设备序列号列表
            scripts: 脚本列表

        Returns:
            Dict: 详细的执行结果
        """
        device_count = len(device_serials)

        # 使用增强的阈值预测
        predicted_threshold = self.threshold_manager.predict_optimal_threshold(device_count)
        auto_threshold = self.threshold_manager.auto_adjust_threshold()

        # 选择更保守的阈值作为最终阈值
        final_threshold = min(predicted_threshold, auto_threshold)

        print(f"📱 设备数量: {device_count}")
        print(f"🎯 预测阈值: {predicted_threshold}")
        print(f"🔧 自动调整阈值: {auto_threshold}")
        print(f"✅ 最终阈值: {final_threshold}")

        # 获取性能建议
        recommendations = self.threshold_manager.get_performance_recommendations()
        print(f"💡 性能建议:")
        for i, rec in enumerate(recommendations, 1):
            print(f"   {i}. {rec}")

        # 预分配账号
        print(f"🔐 预分配账号中...")
        account_allocations = self.account_manager.allocate_account_batch(device_serials)
        # 全或无：只要有设备未分配到账号，则视为失败
        if not account_allocations or len(account_allocations) != len(device_serials):
            missing = [d for d in device_serials if d not in (account_allocations or {})]
            if missing:
                print(f"❌ 账号分配失败，以下设备未分配到账号: {', '.join(missing)}")
            return {
                'success': False,
                'error': '账号分配失败',
                'strategy': 'failed',
                'device_count': device_count,
                'threshold_used': final_threshold
            }

        try:
            start_time = time.time()

            # 动态策略选择
            device_count = len(device_serials)
            if device_count <= final_threshold:
                print(f"🚀 执行策略：无限制并发执行 (设备数 {device_count} ≤ 阈值 {final_threshold})")
                result = self._unlimited_execution_enhanced(device_serials, scripts, account_allocations)
            else:
                print(f"⚙️ 执行策略：智能动态管理 (设备数 {device_count} > 阈值 {final_threshold})")
                result = self._intelligent_execution_enhanced(device_serials, scripts, account_allocations)

            execution_time = time.time() - start_time

            # 记录增强的性能数据
            self._record_enhanced_performance(device_count, execution_time, result)

            # 添加详细的执行信息
            result.update({
                'execution_time': execution_time,
                'device_count': device_count,
                'threshold_used': final_threshold,
                'recommendations': recommendations,
                'account_allocations': len(account_allocations)
            })

            return result

        finally:
            # 释放账号资源
            print(f"🔓 释放账号资源...")
            self.account_manager.release_account_batch(device_serials)

    def _unlimited_execution(self, device_serials: List[str], scripts: List[dict],
                           account_allocations: Dict[str, dict], task_id: Optional[int] = None) -> Dict:
        """
        无限制并发执行：适用于小规模设备

        Args:
            device_serials: 设备序列号列表
            scripts: 脚本列表
            account_allocations: 账号分配结果

        Returns:
            Dict: 执行结果
        """
        print(f"🚀 启动无限制并发模式，处理 {len(device_serials)} 个设备")

        start_time = time.time()
        results = {}
        # 使用线程池以兼容 Celery 子进程上下文
        max_workers = max(1, len(device_serials))
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map = {}
            for device_serial in device_serials:
                account = account_allocations.get(device_serial)
                if not account:
                    print(f"❌ 设备 {device_serial} 没有分配到账号，跳过")
                    continue
                future = executor.submit(_device_worker_with_account, device_serial, scripts, account, None, task_id)
                future_map[future] = device_serial
                print(f"✅ 启动设备 {device_serial} 线程任务")

            for future in concurrent.futures.as_completed(future_map.keys()):
                device_serial = future_map[future]
                try:
                    res = future.result()
                except Exception as e:
                    print(f"❌ 设备 {device_serial} 执行异常: {e}")
                    res = {"success": False, "error": str(e)}
                results[device_serial] = res

        execution_time = time.time() - start_time
        print(f"✅ 无限制并发执行完成，耗时: {execution_time:.2f}秒")

        # 统计成功率
        total_devices = len(device_serials)
        successful_devices = sum(1 for r in results.values() if r.get('success', False))

        return {
            "success": successful_devices == total_devices,
            "execution_time": execution_time,
            "strategy": "unlimited",
            "total_devices": total_devices,
            "successful_devices": successful_devices,
            "success_rate": successful_devices / total_devices if total_devices > 0 else 0,
            "device_results": results
        }

    def _unlimited_execution_enhanced(self, device_serials: List[str], scripts: List[str],
                                    account_allocations: Dict[str, dict]) -> Dict:
        """
        增强版无限制并发执行
        """
        print(f"🚀 启动增强无限制并发模式，处理 {len(device_serials)} 个设备")

        # 监控系统资源变化
        initial_resources = self._evaluate_system_resources()
        print(f"📊 初始系统资源: CPU {initial_resources.cpu_usage:.1f}%, 内存 {initial_resources.memory_usage:.1f}%")

        # 使用线程池并发执行（错峰提交）
        start_time = time.time()
        results = {}
        max_workers = max(1, len(device_serials))
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map = {}
            for i, device_serial in enumerate(device_serials):
                account = account_allocations.get(device_serial)
                if not account:
                    print(f"❌ 设备 {device_serial} 没有分配到账号，跳过")
                    continue
                future = executor.submit(_device_worker_with_account, device_serial, scripts, account, None)
                future_map[future] = device_serial
                print(f"✅ 启动设备 {device_serial} 线程任务")
                if i < len(device_serials) - 1:
                    time.sleep(0.5)

            # 中期资源检查（线程稳定后）
            if len(future_map) > 2:
                time.sleep(5)
                mid_resources = self._evaluate_system_resources()
                print(f"📊 中期系统资源: CPU {mid_resources.cpu_usage:.1f}%, 内存 {mid_resources.memory_usage:.1f}%")
                if mid_resources.cpu_usage > 90 or mid_resources.memory_usage > 90:
                    print(f"⚠️ 系统资源使用率过高，建议下次降低并发数")

            completed_threads = 0
            for future in concurrent.futures.as_completed(future_map.keys()):
                device_serial = future_map[future]
                try:
                    res = future.result()
                except Exception as e:
                    print(f"❌ 设备 {device_serial} 执行异常: {e}")
                    res = {"success": False, "error": str(e)}
                results[device_serial] = res
                completed_threads += 1
                print(f"✅ 线程完成 {completed_threads}/{len(future_map)}")

        execution_time = time.time() - start_time

        # 最终资源检查
        final_resources = self._evaluate_system_resources()
        print(f"📊 最终系统资源: CPU {final_resources.cpu_usage:.1f}%, 内存 {final_resources.memory_usage:.1f}%")

    # 收集结果（已在线程池回调中累积）

        # 统计成功率
        total_devices = len(device_serials)
        successful_devices = sum(1 for r in results.values() if r.get('success', False))

        return {
            "success": successful_devices == total_devices,
            "execution_time": execution_time,
            "strategy": "unlimited_enhanced",
            "total_devices": total_devices,
            "successful_devices": successful_devices,
            "success_rate": successful_devices / total_devices if total_devices > 0 else 0,
            "device_results": results,
            "resource_usage": {
                "initial": {"cpu": initial_resources.cpu_usage, "memory": initial_resources.memory_usage},
                "final": {"cpu": final_resources.cpu_usage, "memory": final_resources.memory_usage}
            }
        }

    def _intelligent_execution(self, device_serials: List[str], scripts: List[dict],
                             account_allocations: Dict[str, dict], task_id: Optional[int] = None) -> Dict:
        """
        智能动态管理：适用于大规模设备

        Args:
            device_serials: 设备序列号列表
            scripts: 脚本列表
            account_allocations: 账号分配结果

        Returns:
            Dict: 执行结果
        """
        # 动态评估系统资源
        resource_status = self._evaluate_system_resources()
        max_concurrent = resource_status.optimal_concurrency

        print(f"⚙️ 智能执行模式，最大并发数: {max_concurrent}")
        print(f"📊 系统资源: CPU {resource_status.cpu_usage:.1f}%, 内存 {resource_status.memory_usage:.1f}%")

        # 实现滚动执行机制
        return self._rolling_execution(device_serials, scripts, account_allocations, max_concurrent, task_id=task_id)

    def _intelligent_execution_enhanced(self, device_serials: List[str], scripts: List[str],
                                      account_allocations: Dict[str, dict]) -> Dict:
        """
        增强版智能动态管理
        """
        # 动态评估系统资源
        resource_status = self._evaluate_system_resources()

        # 基于系统负载和设备数量调整并发数
        device_count = len(device_serials)
        base_concurrent = resource_status.optimal_concurrency

        # 动态调整并发数
        if device_count > 20:
            # 超大规模，更保守
            max_concurrent = min(base_concurrent, 8)
        elif device_count > 10:
            # 大规模，适中
            max_concurrent = min(base_concurrent, 12)
        else:
            # 中等规模，可以更激进
            max_concurrent = base_concurrent

        print(f"⚙️ 增强智能执行模式")
        print(f"📊 系统资源: CPU {resource_status.cpu_usage:.1f}%, 内存 {resource_status.memory_usage:.1f}%")
        print(f"🎯 基础并发数: {base_concurrent}, 调整后并发数: {max_concurrent}")

        # 实现增强的滚动执行机制
        return self._rolling_execution_enhanced(device_serials, scripts, account_allocations, max_concurrent)

    def _rolling_execution(self, device_serials: List[str], scripts: List[dict],
                         account_allocations: Dict[str, dict], max_concurrent: int, task_id: Optional[int] = None) -> Dict:
        """
        滚动执行机制

        Args:
            device_serials: 设备序列号列表
            scripts: 脚本列表
            account_allocations: 账号分配结果
            max_concurrent: 最大并发数

        Returns:
            Dict: 执行结果
        """
        print(f"🔄 启动滚动执行，设备总数: {len(device_serials)}, 最大并发: {max_concurrent}")

        start_time = time.time()
        pending_devices = deque(device_serials)
        completed_results = {}

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_concurrent) as executor:
            running_futures = {}
            completed_count = 0

            while pending_devices or running_futures:
                # 启动新任务直到达到并发限制
                while len(running_futures) < max_concurrent and pending_devices:
                    device_serial = pending_devices.popleft()
                    account = account_allocations.get(device_serial)
                    if account:
                        future = executor.submit(_device_worker_with_account, device_serial, scripts, account, {}, task_id)
                        running_futures[future] = device_serial
                        print(f"🚀 启动设备 {device_serial} ({len(running_futures)}/{max_concurrent})")
                    else:
                        print(f"❌ 设备 {device_serial} 没有分配到账号，跳过")

                # 检查已完成的任务
                if running_futures:
                    done, _ = concurrent.futures.wait(
                        running_futures.keys(),
                        timeout=0.5,
                        return_when=concurrent.futures.FIRST_COMPLETED
                    )

                    for future in done:
                        device_serial = running_futures.pop(future)
                        completed_count += 1

                        try:
                            result = future.result()
                            completed_results[device_serial] = result
                            status = "✅ 成功" if result.get('success') else "❌ 失败"
                            print(f"{status} 设备 {device_serial} 执行完成 ({completed_count}/{len(device_serials)})")
                        except Exception as e:
                            print(f"❌ 设备 {device_serial} 执行异常: {e}")
                            completed_results[device_serial] = {"success": False, "error": str(e)}

        execution_time = time.time() - start_time

        # 统计结果
        total_devices = len(device_serials)
        successful_devices = sum(1 for r in completed_results.values() if r.get('success', False))

        print(f"✅ 滚动执行完成，耗时: {execution_time:.2f}秒，成功率: {successful_devices}/{total_devices}")

        return {
            "success": successful_devices == total_devices,
            "execution_time": execution_time,
            "strategy": "intelligent",
            "total_devices": total_devices,
            "successful_devices": successful_devices,
            "success_rate": successful_devices / total_devices if total_devices > 0 else 0,
            "max_concurrent": max_concurrent,
            "device_results": completed_results
        }

    def _rolling_execution_enhanced(self, device_serials: List[str], scripts: List[str],
                                  account_allocations: Dict[str, dict], max_concurrent: int) -> Dict:
        """
        增强版滚动执行机制 - 包含动态监控和调整
        """
        print(f"🔄 启动增强滚动执行，设备总数: {len(device_serials)}, 最大并发: {max_concurrent}")

        start_time = time.time()
        pending_devices = deque(device_serials)
        completed_results = {}

        # 资源监控数据
        resource_history = []

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_concurrent) as executor:
            running_futures = {}
            completed_count = 0

            while pending_devices or running_futures:
                # 动态资源检查
                if completed_count % 3 == 0 and completed_count > 0:
                    current_resources = self._evaluate_system_resources()
                    resource_history.append({
                        'time': time.time() - start_time,
                        'cpu': current_resources.cpu_usage,
                        'memory': current_resources.memory_usage
                    })

                    # 动态调整并发数
                    if current_resources.cpu_usage > 85 or current_resources.memory_usage > 85:
                        # 系统负载过高，暂停启动新任务
                        print(f"⚠️ 系统负载过高，暂停启动新任务 (CPU: {current_resources.cpu_usage:.1f}%, 内存: {current_resources.memory_usage:.1f}%)")
                        time.sleep(2)
                        continue

                # 启动新任务直到达到并发限制
                while len(running_futures) < max_concurrent and pending_devices:
                    device_serial = pending_devices.popleft()
                    account = account_allocations.get(device_serial)
                    if account:
                        future = executor.submit(_device_worker_with_account, device_serial, scripts, account, {})
                        running_futures[future] = device_serial
                        print(f"🚀 启动设备 {device_serial} ({len(running_futures)}/{max_concurrent})")
                    else:
                        print(f"❌ 设备 {device_serial} 没有分配到账号，跳过")

                # 检查已完成的任务
                if running_futures:
                    done, _ = concurrent.futures.wait(
                        running_futures.keys(),
                        timeout=1.0,
                        return_when=concurrent.futures.FIRST_COMPLETED
                    )

                    for future in done:
                        device_serial = running_futures.pop(future)
                        completed_count += 1

                        try:
                            result = future.result()
                            completed_results[device_serial] = result
                            status = "✅ 成功" if result.get('success') else "❌ 失败"
                            print(f"{status} 设备 {device_serial} 执行完成 ({completed_count}/{len(device_serials)})")
                        except Exception as e:
                            print(f"❌ 设备 {device_serial} 执行异常: {e}")
                            completed_results[device_serial] = {"success": False, "error": str(e)}

        execution_time = time.time() - start_time

        # 统计结果
        total_devices = len(device_serials)
        successful_devices = sum(1 for r in completed_results.values() if r.get('success', False))

        print(f"✅ 增强滚动执行完成，耗时: {execution_time:.2f}秒，成功率: {successful_devices}/{total_devices}")

        return {
            "success": successful_devices == total_devices,
            "execution_time": execution_time,
            "strategy": "intelligent_enhanced",
            "total_devices": total_devices,
            "successful_devices": successful_devices,
            "success_rate": successful_devices / total_devices if total_devices > 0 else 0,
            "max_concurrent": max_concurrent,
            "device_results": completed_results,
            "resource_history": resource_history
        }

    def _record_enhanced_performance(self, device_count: int, execution_time: float, result: Dict):
        """
        记录增强的性能数据
        """
        # 基础性能记录
        self.threshold_manager.record_performance(device_count, execution_time)

        # 记录详细性能指标
        success_rate = result.get('success_rate', 0)
        strategy = result.get('strategy', 'unknown')

        # 计算综合性能分数
        time_score = min(device_count / max(execution_time, 1), 1.0)  # 时间效率
        success_score = success_rate  # 成功率
        comprehensive_score = (time_score * 0.6 + success_score * 0.4)  # 综合分数

        print(f"📈 性能评分: 时间效率={time_score:.3f}, 成功率={success_score:.3f}, 综合={comprehensive_score:.3f}")

        # 可以在这里添加更详细的性能日志记录

    def _evaluate_system_resources(self) -> SystemResourceStatus:
        """
        评估系统资源状态

        Returns:
            SystemResourceStatus: 系统资源状态
        """
        cpu_percent = psutil.cpu_percent(interval=1)
        memory = psutil.virtual_memory()
        memory_percent = memory.percent
        cpu_count = psutil.cpu_count()

        # 动态并发数计算
        if cpu_percent < 50 and memory_percent < 70:
            optimal_concurrency = min(16, cpu_count * 2)
        elif cpu_percent < 70 and memory_percent < 80:
            optimal_concurrency = min(12, cpu_count)
        elif cpu_percent < 80 and memory_percent < 90:
            optimal_concurrency = min(8, cpu_count // 2)
        else:
            optimal_concurrency = 4

        max_safe_concurrency = min(optimal_concurrency * 2, 20)

        return SystemResourceStatus(
            cpu_usage=cpu_percent,
            memory_usage=memory_percent,
            optimal_concurrency=optimal_concurrency,
            max_safe_concurrency=max_safe_concurrency
        )

# 便捷函数，提供与现有代码的兼容接口
def replay_scripts_on_devices_hybrid(device_serials: List[str], scripts: List[dict],
                                   strategy: str = "hybrid", task_id: Optional[int] = None) -> tuple:
    """
    智能混合执行策略接口

    Args:
        device_serials: 设备序列号列表
        scripts: 脚本列表
        strategy: 执行策略 ("hybrid", "unlimited", "intelligent")

    Returns:
        tuple: (results_dict, device_report_dirs_list)
    """
    executor = OptimizedHybridExecutor()

    if strategy == "unlimited":
        # 强制使用无限制并发
        result = executor._unlimited_execution(device_serials, scripts, {}, task_id=task_id)
    elif strategy == "intelligent":
        # 强制使用智能管理
        account_allocations = executor.account_manager.allocate_account_batch(device_serials)
        try:
            result = executor._intelligent_execution(device_serials, scripts, account_allocations, task_id=task_id)
        finally:
            executor.account_manager.release_account_batch(device_serials)
    else:
        # 默认混合策略
        result = executor.execute_multi_device(device_serials, scripts, task_id=task_id)

    # 兼容现有接口，返回结果和设备报告目录
    device_results = result.get('device_results', {})
    device_report_dirs = []

    # 提取设备报告目录
    for device_serial, device_result in device_results.items():
        report_dir = device_result.get('device_report_dir')
        if report_dir:
            from pathlib import Path
            device_report_dirs.append(Path(report_dir))

    return device_results, device_report_dirs


if __name__ == "__main__":
    print("=== 智能混合执行器测试 ===")

    try:
        # 初始化执行器
        executor = OptimizedHybridExecutor()

        # 模拟设备和脚本
        device_serials = ["test_device_1", "test_device_2", "test_device_3"]
        scripts = [
            {
                'path': 'testcase/test_script.json',
                'loop_count': 1,
                'max_duration': None
            }
        ]

        print(f"\n🧪 测试设备: {device_serials}")
        print(f"📜 测试脚本: {[s['path'] for s in scripts]}")

        # 测试混合策略
        print(f"\n🚀 执行混合策略测试...")
        result = executor.execute_multi_device(device_serials, scripts)

        print(f"\n📊 执行结果:")
        print(f"   成功: {result.get('success')}")
        print(f"   策略: {result.get('strategy')}")
        print(f"   耗时: {result.get('execution_time', 0):.2f}秒")
        print(f"   成功率: {result.get('success_rate', 0):.2%}")

        # 显示阈值管理器状态
        print(f"\n📈 阈值管理器状态:")
        summary = executor.threshold_manager.get_performance_summary()
        for key, value in summary.items():
            print(f"   {key}: {value}")

    except Exception as e:
        print(f"❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()

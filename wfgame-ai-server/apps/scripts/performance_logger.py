#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
性能日志记录器
提供统一的日志接口和性能指标收集功能
"""

import os
import time
import logging
import psutil
from datetime import datetime
from typing import Dict, List, Optional, Any
from collections import defaultdict
from contextlib import contextmanager


class PerformanceMetrics:
    """性能指标收集器"""
    
    def __init__(self):
        self.metrics = defaultdict(list)
        self.counters = defaultdict(int)
        self.timers = {}
        
    def record_time(self, operation: str, duration: float):
        """记录操作耗时"""
        self.metrics[f"{operation}_time"].append(duration)
        
    def increment_counter(self, name: str, value: int = 1):
        """增加计数器"""
        self.counters[name] += value
        
    def get_average_time(self, operation: str) -> float:
        """获取平均耗时"""
        times = self.metrics.get(f"{operation}_time", [])
        return sum(times) / len(times) if times else 0.0
    
    def get_total_time(self, operation: str) -> float:
        """获取总耗时"""
        times = self.metrics.get(f"{operation}_time", [])
        return sum(times)
    
    def get_count(self, name: str) -> int:
        """获取计数"""
        return self.counters.get(name, 0)
    
    def get_success_rate(self, operation: str) -> float:
        """获取成功率"""
        total = self.get_count(f"{operation}_total")
        success = self.get_count(f"{operation}_success")
        return (success / total * 100) if total > 0 else 0.0
    
    def get_summary(self) -> Dict[str, Any]:
        """获取性能摘要"""
        summary = {
            'timings': {},
            'counters': dict(self.counters),
            'success_rates': {}
        }
        
        # 计算各操作的平均耗时
        for key in self.metrics:
            if key.endswith('_time'):
                operation = key[:-5]  # 移除'_time'后缀
                summary['timings'][operation] = {
                    'average': self.get_average_time(operation),
                    'total': self.get_total_time(operation),
                    'count': len(self.metrics[key]),
                    'min': min(self.metrics[key]) if self.metrics[key] else 0,
                    'max': max(self.metrics[key]) if self.metrics[key] else 0
                }
        
        # 计算成功率
        operations = set()
        for key in self.counters:
            if key.endswith('_total'):
                operations.add(key[:-6])
        
        for op in operations:
            summary['success_rates'][op] = self.get_success_rate(op)
        
        return summary
    
    def reset(self):
        """重置所有指标"""
        self.metrics.clear()
        self.counters.clear()
        self.timers.clear()


class PerformanceLogger:
    """性能日志记录器（单例模式）"""
    
    _instance = None
    _lock = None
    
    def __new__(cls):
        if cls._instance is None:
            import threading
            if cls._lock is None:
                cls._lock = threading.Lock()
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
            
        self._initialized = True
        self.metrics = PerformanceMetrics()
        self.process = psutil.Process(os.getpid())
        
        # 配置日志记录器
        self.logger = logging.getLogger('performance')
        if not self.logger.handlers:
            handler = logging.StreamHandler()
            formatter = logging.Formatter(
                '%(asctime)s - [性能] - %(levelname)s - %(message)s',
                datefmt='%H:%M:%S'
            )
            handler.setFormatter(formatter)
            self.logger.addHandler(handler)
            self.logger.setLevel(logging.INFO)
    
    @contextmanager
    def measure_time(self, operation: str, log_result: bool = True):
        """
        测量操作耗时的上下文管理器
        
        Args:
            operation: 操作名称
            log_result: 是否记录日志
            
        Example:
            with logger.measure_time('screenshot'):
                take_screenshot()
        """
        start_time = time.time()
        try:
            yield
        finally:
            duration = time.time() - start_time
            self.metrics.record_time(operation, duration)
            
            if log_result:
                self.logger.info(f"⏱️ {operation} 耗时: {duration:.3f}秒")
    
    def log_step_performance(self, step_name: str, metrics: Dict[str, float]):
        """
        记录步骤性能指标
        
        Args:
            step_name: 步骤名称
            metrics: 性能指标字典 {'screenshot': 0.5, 'yolo': 0.3, 'ocr': 0.2}
        """
        total_time = sum(metrics.values())
        
        self.logger.info(f"📊 步骤 [{step_name}] 性能分析:")
        self.logger.info(f"   总耗时: {total_time:.3f}秒")
        
        for operation, duration in sorted(metrics.items(), key=lambda x: x[1], reverse=True):
            percentage = (duration / total_time * 100) if total_time > 0 else 0
            self.logger.info(f"   - {operation}: {duration:.3f}秒 ({percentage:.1f}%)")
            self.metrics.record_time(f"step_{operation}", duration)
    
    def log_resource_usage(self, context: str = ""):
        """
        记录资源使用情况
        
        Args:
            context: 上下文信息
        """
        try:
            # CPU使用率
            cpu_percent = self.process.cpu_percent(interval=0.1)
            
            # 内存使用
            memory_info = self.process.memory_info()
            memory_mb = memory_info.rss / 1024 / 1024
            
            # GPU使用率（如果可用）
            gpu_info = self._get_gpu_info()
            
            prefix = f"[{context}] " if context else ""
            self.logger.info(f"💻 {prefix}资源使用:")
            self.logger.info(f"   CPU: {cpu_percent:.1f}%")
            self.logger.info(f"   内存: {memory_mb:.1f}MB")
            
            if gpu_info:
                self.logger.info(f"   GPU利用率: {gpu_info['utilization']}%")
                self.logger.info(f"   GPU显存: {gpu_info['memory_used']}/{gpu_info['memory_total']}MB")
                
        except Exception as e:
            self.logger.warning(f"⚠️ 获取资源使用信息失败: {e}")
    
    def _get_gpu_info(self) -> Optional[Dict[str, Any]]:
        """获取GPU信息"""
        try:
            import pynvml
            pynvml.nvmlInit()
            handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            
            utilization = pynvml.nvmlDeviceGetUtilizationRates(handle)
            memory = pynvml.nvmlDeviceGetMemoryInfo(handle)
            
            return {
                'utilization': utilization.gpu,
                'memory_used': memory.used / 1024 / 1024,
                'memory_total': memory.total / 1024 / 1024
            }
        except Exception:
            return None
    
    def log_success(self, operation: str, message: str = ""):
        """记录成功操作（INFO级别）"""
        self.metrics.increment_counter(f"{operation}_total")
        self.metrics.increment_counter(f"{operation}_success")
        
        msg = f"✅ {operation}"
        if message:
            msg += f": {message}"
        self.logger.info(msg)
    
    def log_failure(self, operation: str, reason: str, retry_count: int = 0):
        """记录失败操作（WARNING级别）"""
        self.metrics.increment_counter(f"{operation}_total")
        self.metrics.increment_counter(f"{operation}_failure")
        self.metrics.increment_counter(f"failure_reason_{reason}")
        
        if retry_count > 0:
            self.logger.warning(f"⚠️ {operation} 失败 (重试{retry_count}次): {reason}")
        else:
            self.logger.warning(f"⚠️ {operation} 失败: {reason}")
    
    def log_error(self, operation: str, error: Exception):
        """记录错误（ERROR级别）"""
        self.metrics.increment_counter(f"{operation}_total")
        self.metrics.increment_counter(f"{operation}_error")
        
        self.logger.error(f"❌ {operation} 错误: {error}")
        
        # 记录详细堆栈信息
        import traceback
        self.logger.debug(traceback.format_exc())
    
    def log_retry(self, operation: str, attempt: int, max_attempts: int, reason: str = ""):
        """记录重试操作（INFO级别）"""
        self.metrics.increment_counter(f"{operation}_retry")
        
        msg = f"🔄 {operation} 重试 {attempt}/{max_attempts}"
        if reason:
            msg += f": {reason}"
        self.logger.info(msg)
    
    def log_summary(self, context: str = ""):
        """
        记录性能摘要
        
        Args:
            context: 上下文信息（如设备序列号、任务ID等）
        """
        summary = self.metrics.get_summary()
        
        prefix = f"[{context}] " if context else ""
        self.logger.info(f"\n{'='*60}")
        self.logger.info(f"📈 {prefix}性能摘要")
        self.logger.info(f"{'='*60}")
        
        # 耗时统计
        if summary['timings']:
            self.logger.info("\n⏱️ 耗时统计:")
            for operation, stats in sorted(summary['timings'].items(), 
                                          key=lambda x: x[1]['total'], 
                                          reverse=True):
                self.logger.info(
                    f"   {operation}: "
                    f"总计{stats['total']:.2f}秒, "
                    f"平均{stats['average']:.3f}秒, "
                    f"次数{stats['count']}, "
                    f"范围[{stats['min']:.3f}-{stats['max']:.3f}]秒"
                )
        
        # 成功率统计
        if summary['success_rates']:
            self.logger.info("\n📊 成功率统计:")
            for operation, rate in sorted(summary['success_rates'].items(), 
                                         key=lambda x: x[1]):
                total = self.metrics.get_count(f"{operation}_total")
                success = self.metrics.get_count(f"{operation}_success")
                failure = self.metrics.get_count(f"{operation}_failure")
                
                self.logger.info(
                    f"   {operation}: {rate:.1f}% "
                    f"(成功{success}/{total}, 失败{failure})"
                )
        
        # 失败原因分布
        failure_reasons = {k: v for k, v in summary['counters'].items() 
                          if k.startswith('failure_reason_')}
        if failure_reasons:
            self.logger.info("\n⚠️ 失败原因分布:")
            for reason, count in sorted(failure_reasons.items(), 
                                       key=lambda x: x[1], 
                                       reverse=True):
                reason_name = reason.replace('failure_reason_', '')
                self.logger.info(f"   {reason_name}: {count}次")
        
        # 重试统计
        retry_ops = {k: v for k, v in summary['counters'].items() 
                    if k.endswith('_retry')}
        if retry_ops:
            self.logger.info("\n🔄 重试统计:")
            total_retries = sum(retry_ops.values())
            self.logger.info(f"   总重试次数: {total_retries}")
            for op, count in sorted(retry_ops.items(), 
                                   key=lambda x: x[1], 
                                   reverse=True):
                op_name = op.replace('_retry', '')
                self.logger.info(f"   {op_name}: {count}次")
        
        self.logger.info(f"{'='*60}\n")
    
    def reset_metrics(self):
        """重置性能指标"""
        self.metrics.reset()
        self.logger.info("🔄 性能指标已重置")


# 全局单例获取函数
def get_performance_logger() -> PerformanceLogger:
    """获取性能日志记录器单例"""
    return PerformanceLogger()


# 便捷函数
def log_info(message: str):
    """记录信息日志（INFO级别）"""
    logger = logging.getLogger('performance')
    logger.info(f"ℹ️ {message}")


def log_warning(message: str):
    """记录警告日志（WARNING级别）- 仅用于真正的警告"""
    logger = logging.getLogger('performance')
    logger.warning(f"⚠️ {message}")


def log_error(message: str, error: Optional[Exception] = None):
    """记录错误日志（ERROR级别）"""
    logger = logging.getLogger('performance')
    msg = f"❌ {message}"
    if error:
        msg += f": {error}"
    logger.error(msg)


def log_success(message: str):
    """记录成功日志（INFO级别）- 不使用WARNING"""
    logger = logging.getLogger('performance')
    logger.info(f"✅ {message}")

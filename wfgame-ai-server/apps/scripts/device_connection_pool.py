#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
设备连接池管理器
实现设备状态缓存、连接复用和统一初始化，避免重复检查和初始化
"""

import os
import time
import threading
from typing import Dict, Optional, Any
from datetime import datetime, timedelta
from airtest.core.api import connect_device


class DeviceInfo:
    """设备信息缓存类"""
    
    def __init__(self, serial: str):
        self.serial = serial
        self.device = None  # Airtest设备对象
        self.model = None  # 设备型号
        self.android_version = None  # Android版本
        self.screen_size = None  # 屏幕尺寸
        self.is_screen_on = None  # 屏幕状态
        self.is_unlocked = None  # 解锁状态
        self.last_check_time = None  # 最后检查时间
        self.last_screenshot = None  # 最后一次截图（PIL Image对象）
        self.last_screenshot_time = None  # 最后截图时间
        self.init_success = False  # 初始化是否成功
        self.error_message = None  # 错误信息
        
    def is_cache_valid(self, max_age_seconds: int = 300) -> bool:
        """检查缓存是否有效（默认5分钟）"""
        if self.last_check_time is None:
            return False
        age = (datetime.now() - self.last_check_time).total_seconds()
        return age < max_age_seconds
    
    def is_screenshot_valid(self, max_age_seconds: int = 2) -> bool:
        """检查截图缓存是否有效（默认2秒）"""
        if self.last_screenshot is None or self.last_screenshot_time is None:
            return False
        age = (datetime.now() - self.last_screenshot_time).total_seconds()
        return age < max_age_seconds


class DeviceConnectionPool:
    """设备连接池管理器（单例模式）"""
    
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
        if self._initialized:
            return
            
        self._initialized = True
        self.devices: Dict[str, DeviceInfo] = {}
        self.pool_lock = threading.Lock()
        
        pid = os.getpid()
        print(f"🔧 [进程PID:{pid}] 初始化设备连接池...")
    
    def get_device(self, serial: str, force_refresh: bool = False) -> Optional[DeviceInfo]:
        """
        获取设备信息，优先从缓存读取
        
        Args:
            serial: 设备序列号
            force_refresh: 是否强制刷新缓存
            
        Returns:
            DeviceInfo对象，失败返回None
        """
        with self.pool_lock:
            # 检查缓存
            if not force_refresh and serial in self.devices:
                device_info = self.devices[serial]
                if device_info.is_cache_valid():
                    pid = os.getpid()
                    print(f"✅ [进程PID:{pid}] 设备 {serial} 使用缓存信息")
                    return device_info
            
            # 初始化或刷新设备信息
            return self._initialize_device(serial)
    
    def _initialize_device(self, serial: str) -> Optional[DeviceInfo]:
        """
        初始化设备信息（内部方法，需要持有锁）
        
        Args:
            serial: 设备序列号
            
        Returns:
            DeviceInfo对象，失败返回None
        """
        pid = os.getpid()
        print(f"🔄 [进程PID:{pid}] 初始化设备 {serial}...")
        
        device_info = DeviceInfo(serial)
        start_time = time.time()
        
        try:
            # 1. 连接设备
            device_info.device = connect_device(f"Android:///{serial}")
            
            # 2. 获取设备基本信息
            try:
                import subprocess
                
                # 获取设备型号
                result = subprocess.run(
                    f"adb -s {serial} shell getprop ro.product.model",
                    shell=True, capture_output=True, text=True,
                    encoding='utf-8', errors='ignore', timeout=5
                )
                if result.returncode == 0:
                    device_info.model = result.stdout.strip()
                
                # 获取Android版本
                result = subprocess.run(
                    f"adb -s {serial} shell getprop ro.build.version.release",
                    shell=True, capture_output=True, text=True,
                    encoding='utf-8', errors='ignore', timeout=5
                )
                if result.returncode == 0:
                    device_info.android_version = result.stdout.strip()
                
                # 获取屏幕尺寸
                result = subprocess.run(
                    f"adb -s {serial} shell wm size",
                    shell=True, capture_output=True, text=True,
                    encoding='utf-8', errors='ignore', timeout=5
                )
                if result.returncode == 0 and "Physical size:" in result.stdout:
                    import re
                    match = re.search(r'(\d+)x(\d+)', result.stdout)
                    if match:
                        device_info.screen_size = (int(match.group(1)), int(match.group(2)))
                
            except Exception as e:
                print(f"⚠️ [进程PID:{pid}] 设备 {serial} 获取基本信息失败: {e}")
            
            # 3. 检查屏幕状态（不阻塞）
            try:
                self._check_screen_state(device_info)
            except Exception as e:
                print(f"⚠️ [进程PID:{pid}] 设备 {serial} 屏幕状态检查失败: {e}")
            
            # 标记初始化成功
            device_info.init_success = True
            device_info.last_check_time = datetime.now()
            
            # 保存到缓存
            self.devices[serial] = device_info
            
            elapsed = time.time() - start_time
            print(f"✅ [进程PID:{pid}] 设备 {serial} 初始化完成")
            print(f"   型号: {device_info.model or '未知'}")
            print(f"   Android版本: {device_info.android_version or '未知'}")
            print(f"   屏幕尺寸: {device_info.screen_size or '未知'}")
            print(f"   初始化耗时: {elapsed:.2f}秒")
            
            return device_info
            
        except Exception as e:
            device_info.init_success = False
            device_info.error_message = str(e)
            
            pid = os.getpid()
            print(f"❌ [进程PID:{pid}] 设备 {serial} 初始化失败: {e}")
            
            return None
    
    def _check_screen_state(self, device_info: DeviceInfo):
        """
        检查设备屏幕状态（内部方法）
        
        Args:
            device_info: 设备信息对象
        """
        import subprocess
        
        try:
            # 检查屏幕是否打开
            result = subprocess.run(
                f"adb -s {device_info.serial} shell dumpsys power",
                shell=True, capture_output=True, text=True,
                encoding='utf-8', errors='ignore', timeout=5
            )
            
            if result.returncode == 0:
                output = result.stdout
                device_info.is_screen_on = (
                    "mWakefulness=Awake" in output or
                    "mHoldingDisplaySuspendBlocker=true" in output
                )
            
            # 检查是否解锁
            result = subprocess.run(
                f"adb -s {device_info.serial} shell dumpsys window",
                shell=True, capture_output=True, text=True,
                encoding='utf-8', errors='ignore', timeout=5
            )
            
            if result.returncode == 0:
                output = result.stdout
                device_info.is_unlocked = not (
                    "mDreamingLockscreen=true" in output or
                    "KeyguardController" in output
                )
                
        except Exception as e:
            print(f"⚠️ 检查设备 {device_info.serial} 屏幕状态异常: {e}")
    
    def cache_screenshot(self, serial: str, screenshot):
        """
        缓存截图
        
        Args:
            serial: 设备序列号
            screenshot: PIL Image对象
        """
        with self.pool_lock:
            if serial in self.devices:
                device_info = self.devices[serial]
                device_info.last_screenshot = screenshot
                device_info.last_screenshot_time = datetime.now()
    
    def get_cached_screenshot(self, serial: str, max_age_seconds: int = 2):
        """
        获取缓存的截图
        
        Args:
            serial: 设备序列号
            max_age_seconds: 最大缓存时间（秒）
            
        Returns:
            PIL Image对象，如果缓存无效返回None
        """
        with self.pool_lock:
            if serial in self.devices:
                device_info = self.devices[serial]
                if device_info.is_screenshot_valid(max_age_seconds):
                    return device_info.last_screenshot
        return None
    
    def invalidate_screenshot(self, serial: str):
        """
        使截图缓存失效（例如执行了点击操作后）
        
        Args:
            serial: 设备序列号
        """
        with self.pool_lock:
            if serial in self.devices:
                device_info = self.devices[serial]
                device_info.last_screenshot = None
                device_info.last_screenshot_time = None
    
    def refresh_device(self, serial: str) -> Optional[DeviceInfo]:
        """
        强制刷新设备信息
        
        Args:
            serial: 设备序列号
            
        Returns:
            DeviceInfo对象，失败返回None
        """
        return self.get_device(serial, force_refresh=True)
    
    def remove_device(self, serial: str):
        """
        从连接池中移除设备
        
        Args:
            serial: 设备序列号
        """
        with self.pool_lock:
            if serial in self.devices:
                del self.devices[serial]
                pid = os.getpid()
                print(f"🗑️ [进程PID:{pid}] 设备 {serial} 已从连接池移除")
    
    def clear_pool(self):
        """清空连接池"""
        with self.pool_lock:
            self.devices.clear()
            pid = os.getpid()
            print(f"🗑️ [进程PID:{pid}] 设备连接池已清空")
    
    def get_pool_stats(self) -> Dict[str, Any]:
        """
        获取连接池统计信息
        
        Returns:
            统计信息字典
        """
        with self.pool_lock:
            return {
                'total_devices': len(self.devices),
                'initialized_devices': sum(1 for d in self.devices.values() if d.init_success),
                'devices': [
                    {
                        'serial': d.serial,
                        'model': d.model,
                        'android_version': d.android_version,
                        'init_success': d.init_success,
                        'cache_valid': d.is_cache_valid(),
                        'has_screenshot': d.last_screenshot is not None
                    }
                    for d in self.devices.values()
                ]
            }


# 全局单例获取函数
def get_device_connection_pool() -> DeviceConnectionPool:
    """获取设备连接池单例"""
    return DeviceConnectionPool()

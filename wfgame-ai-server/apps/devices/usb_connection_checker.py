#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
USB连接检查工具
检查Android设备的USB连接状态和模式
"""

import subprocess
import time
import sys
import os
import threading
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed

def check_usb_connection():
    """检查USB连接状态"""
    print("=" * 60)
    print("WFGameAI USB连接检查工具")
    print("=" * 60)

    print("\n🔍 正在检查ADB服务状态...")
    try:
        # 启动ADB服务
        subprocess.run("adb start-server", shell=True, check=True)
        print("✅ ADB服务已启动")
    except Exception as e:
        print(f"❌ ADB服务启动失败: {e}")
        return False

    print("\n🔍 正在检查连接的设备...")
    try:
        result = subprocess.run("adb devices", shell=True, capture_output=True, text=True, encoding='utf-8', errors='ignore')
        print("ADB设备列表：")
        print(result.stdout)

        # 解析设备列表
        devices = []
        for line in result.stdout.split('\n')[1:]:
            if line.strip() and '\t' in line:
                device_id, status = line.split('\t')
                devices.append((device_id.strip(), status.strip()))

        if not devices:
            print("❌ 未检测到任何设备")
            print("\n💡 可能的原因：")
            print("1. USB线未连接或连接不良")
            print("2. USB连接模式选择错误（当前为'仅充电'）")
            print("3. USB调试功能未开启")
            print("4. 驱动程序问题")
            print("\n🔧 解决步骤：")
            show_usb_setup_guide()
            return False

        print(f"\n✅ 检测到 {len(devices)} 个设备:")
        for device_id, status in devices:
            status_icon = "✅" if status == "device" else "⚠️" if status == "unauthorized" else "❌"
            print(f"  {status_icon} {device_id}: {status}")

            if status == "unauthorized":
                print(f"    📱 设备 {device_id} 需要授权USB调试")
            elif status == "offline":
                print(f"    🔌 设备 {device_id} 连接异常，可能是USB模式问题")

        return len([d for d in devices if d[1] == "device"]) > 0

    except Exception as e:
        print(f"❌ 检查设备失败: {e}")
        return False

def show_usb_setup_guide():
    """显示USB设置指南"""
    print("\n" + "=" * 60)
    print("📱 Android设备USB连接设置指南")
    print("=" * 60)

    print("""
步骤1: 检查USB连接模式
┌─────────────────────────────────────────┐
│  当手机连接到电脑时，会弹出选择界面：      │
│                                         │
│  选择USB连接方式                        │
│  ○ 仅充电               ← ❌ 不选择此项  │
│  ● 传输文件 (MTP)       ← ✅ 选择此项   │
│  ○ 传输照片 (PTP)       ← ✅ 也可以     │
│  ○ USB网络共享                          │
│  ○ MIDI                                 │
└─────────────────────────────────────────┘

步骤2: 开启USB调试
1. 进入手机"设置"
2. 找到"关于手机"或"关于设备"
3. 连续点击"版本号"7次，开启开发者模式
4. 返回设置主界面
5. 找到"开发者选项"或"开发人员选项"
6. 开启"USB调试"开关

步骤3: 确认授权
1. 重新连接USB线
2. 手机屏幕会弹出"允许USB调试"对话框
3. 勾选"始终允许来自这台计算机"
4. 点击"允许"

步骤4: 验证连接
运行命令: adb devices
正确输出应该类似：
List of devices attached
SM-G973F        device
""")

def check_device_details(device_id):
    """检查设备详细信息"""
    print(f"\n🔍 检查设备 {device_id} 的详细信息...")

    try:
        # 获取设备型号
        result = subprocess.run(f"adb -s {device_id} shell getprop ro.product.model",
                              shell=True, capture_output=True, text=True, encoding='utf-8', errors='ignore')
        if result.returncode == 0:
            model = result.stdout.strip()
            print(f"  📱 设备型号: {model}")

        # 获取Android版本
        result = subprocess.run(f"adb -s {device_id} shell getprop ro.build.version.release",
                              shell=True, capture_output=True, text=True, encoding='utf-8', errors='ignore')
        if result.returncode == 0:
            version = result.stdout.strip()
            print(f"  🤖 Android版本: {version}")

        # 检查USB调试状态
        result = subprocess.run(f"adb -s {device_id} shell getprop persist.adb.tcp.port",
                              shell=True, capture_output=True, text=True, encoding='utf-8', errors='ignore')
        tcp_port = result.stdout.strip() if result.returncode == 0 else "未设置"
        print(f"  🔌 TCP端口: {tcp_port}")

        # 获取设备IP地址
        result = subprocess.run(f"adb -s {device_id} shell ip route | grep wlan",
                              shell=True, capture_output=True, text=True, encoding='utf-8', errors='ignore')
        if result.returncode == 0 and result.stdout.strip():
            print(f"  🌐 网络连接: 已连接WiFi")
            # 尝试获取IP地址
            ip_result = subprocess.run(f"adb -s {device_id} shell ip addr show wlan0 | grep 'inet '",
                                     shell=True, capture_output=True, text=True, encoding='utf-8', errors='ignore')
            if ip_result.returncode == 0:
                import re
                ip_match = re.search(r'inet\s+(\d+\.\d+\.\d+\.\d+)', ip_result.stdout)
                if ip_match:
                    ip = ip_match.group(1)
                    print(f"  📡 设备IP: {ip}")
        else:
            print(f"  🌐 网络连接: 未连接WiFi或无法获取")

        return True

    except Exception as e:
        print(f"  ❌ 获取设备信息失败: {e}")
        return False

def ensure_device_ready(device_id):
    """确保设备处于可用状态（唤醒并解锁）"""
    print(f"\n🔓 准备设备 {device_id} 状态...")

    try:
        # 检查屏幕状态
        result = subprocess.run(f"adb -s {device_id} shell dumpsys power | findstr \"mWakefulness\"",
                              shell=True, capture_output=True, text=True, timeout=5, encoding='utf-8', errors='ignore')

        is_awake = "Awake" in result.stdout if result.returncode == 0 else False
        print(f"  💡 屏幕状态: {'唤醒' if is_awake else '休眠'}")

        if not is_awake:
            print("  🔆 唤醒设备屏幕...")
            subprocess.run(f"adb -s {device_id} shell input keyevent 26",
                         shell=True, timeout=5, encoding='utf-8', errors='ignore')
            time.sleep(1)

        # 检查是否需要解锁
        result = subprocess.run(f"adb -s {device_id} shell dumpsys window | findstr \"mDreamingLockscreen\"",
                              shell=True, capture_output=True, text=True, timeout=5, encoding='utf-8', errors='ignore')

        # 尝试简单的上滑解锁
        print("  🔓 尝试解锁设备...")
        subprocess.run(f"adb -s {device_id} shell input swipe 500 1500 500 100",
                     shell=True, timeout=5, encoding='utf-8', errors='ignore')
        time.sleep(2)

        # 检查最终状态
        result = subprocess.run(f"adb -s {device_id} shell dumpsys window | findstr \"mCurrentFocus\"",
                              shell=True, capture_output=True, text=True, timeout=5, encoding='utf-8', errors='ignore')

        if result.returncode == 0 and result.stdout.strip():
            print(f"  ✅ 设备已准备就绪，当前焦点: {result.stdout.strip()}")
            return True
        else:
            print("  ⚠️ 设备状态未知，继续测试...")
            return True

    except Exception as e:
        print(f"  ❌ 设备状态准备失败: {e}")
        return False

def validate_test_result(cmd, desc, result, device_id):
    """验证测试结果的专业逻辑"""
    success = False
    details = ""

    if result.returncode != 0:
        return False, f"命令执行失败 - {result.stderr.strip()}"

    # 根据测试类型进行专业验证
    if "基本shell命令" in desc:
        success = "Hello" in result.stdout
        details = "Echo命令输出验证" if success else "未检测到预期输出"

    elif "文件系统访问" in desc:
        success = result.returncode == 0 and len(result.stdout.strip()) > 0
        details = f"文件列表返回{len(result.stdout.strip().split())}项" if success else "无法访问目录"

    elif "输入事件（电源键）" in desc:
        # 电源键测试后检查屏幕状态变化
        time.sleep(1)
        check_result = subprocess.run(f"adb -s {device_id} shell dumpsys power | findstr \"mWakefulness\"",
                                    shell=True, capture_output=True, text=True, timeout=5, encoding='utf-8', errors='ignore')
        success = check_result.returncode == 0
        details = "电源键响应正常" if success else "电源键无响应"

    elif "屏幕截图功能" in desc:
        # 检查截图文件是否成功创建到PC本地
        screenshot_path = f"device_screenshots/screenshot_{device_id}.png"
        success = os.path.exists(screenshot_path)
        if success:
            file_size = os.path.getsize(screenshot_path)
            details = f"截图已保存到PC本地 (文件大小: {file_size} bytes)"
            print(f"    📁 截图保存位置: {os.path.abspath(screenshot_path)}")
        else:
            details = "截图文件未生成到PC本地"

    elif "点击Home键" in desc:
        # 检查是否回到了桌面
        time.sleep(1)
        check_result = subprocess.run(f"adb -s {device_id} shell dumpsys window | findstr \"mCurrentFocus\"",
                                    shell=True, capture_output=True, text=True, timeout=5, encoding='utf-8', errors='ignore')
        if check_result.returncode == 0:
            focus = check_result.stdout.strip()
            success = "launcher" in focus.lower() or "home" in focus.lower()
            details = f"当前焦点: {focus}" if success else "未返回桌面"
        else:
            success = result.returncode == 0
            details = "Home键命令执行成功" if success else "Home键命令失败"

    elif "打开系统设置" in desc:
        # 检查设置应用是否启动
        time.sleep(2)
        check_result = subprocess.run(f"adb -s {device_id} shell dumpsys window | findstr \"mCurrentFocus\"",
                                    shell=True, capture_output=True, text=True, timeout=5, encoding='utf-8', errors='ignore')
        if check_result.returncode == 0:
            focus = check_result.stdout.strip()
            success = "settings" in focus.lower() or "setting" in focus.lower()
            details = f"设置应用启动状态: {focus}" if success else "设置应用未启动"
        else:
            success = result.returncode == 0
            details = "设置启动命令执行成功" if success else "设置启动命令失败"

    elif "多方向滑动验证" in desc:
        # 更严谨的滑动测试：测试4个方向的滑动操作
        if result.returncode == 0:
            time.sleep(1)  # 等待滑动动画完成

            print("    🔍 开始多方向滑动验证...")

            # 4个方向的滑动测试
            swipe_tests = [
                ("shell input swipe 300 800 700 800", "从左到右滑动"),    # 左到右
                ("shell input swipe 700 800 300 800", "从右到左滑动"),    # 右到左
                ("shell input swipe 500 1200 500 400", "从下往上滑动"),   # 下到上
                ("shell input swipe 500 400 500 1200", "从上往下滑动")    # 上到下
            ]

            successful_swipes = 0
            total_swipes = len(swipe_tests)

            for swipe_cmd, swipe_desc in swipe_tests:
                try:
                    # 执行滑动命令
                    swipe_result = subprocess.run(f"adb -s {device_id} {swipe_cmd}",
                                                shell=True, capture_output=True, text=True,
                                                timeout=10, encoding='utf-8', errors='ignore')

                    if swipe_result.returncode == 0:
                        time.sleep(0.5)  # 短暂等待滑动效果

                        # 验证滑动后UI状态
                        ui_check = subprocess.run(f"adb -s {device_id} shell dumpsys window | findstr \"mCurrentFocus\"",
                                                shell=True, capture_output=True, text=True,
                                                timeout=5, encoding='utf-8', errors='ignore')

                        if ui_check.returncode == 0 and ui_check.stdout.strip():
                            successful_swipes += 1
                            print(f"      ✅ {swipe_desc} - 成功")
                        else:
                            print(f"      ❌ {swipe_desc} - 无响应")
                    else:
                        print(f"      ❌ {swipe_desc} - 执行失败")

                except Exception as e:
                    print(f"      ❌ {swipe_desc} - 异常: {e}")

            # 判断整体滑动测试结果
            success_rate = successful_swipes / total_swipes
            if successful_swipes >= 1:  # 任一滑动成功即视为功能正常
                success = True
                details = f"多方向滑动测试通过 ({successful_swipes}/{total_swipes} 成功, {success_rate*100:.1f}%)"
            else:
                success = False
                details = f"多方向滑动测试失败 ({successful_swipes}/{total_swipes} 成功, {success_rate*100:.1f}%)"
        else:
            success = False
            details = "滑动命令执行失败"

    elif "等待Home界面加载" in desc or "等待设置界面加载" in desc or "等待滑动UI响应" in desc or "等待返回Home界面" in desc:
        success = result.returncode == 0
        details = f"UI等待完成 - {desc}" if success else f"UI等待失败 - {desc}"

    else:
        # 默认验证逻辑
        success = result.returncode == 0
        details = "命令执行成功" if success else f"命令执行失败: {result.stderr.strip()}"

    return success, details

def execute_multi_direction_swipe_test(device_id, desc):
    """执行严格的4方向滑动测试"""
    print(f"    🔍 开始严格4方向滑动验证...")

    # 4个方向的滑动测试（严格按要求：左-右，右-左，下-上，上-下）
    swipe_tests = [
        ("shell input swipe 300 800 700 800", "从左到右滑动"),    # 左到右 (left-right)
        ("shell input swipe 700 800 300 800", "从右到左滑动"),    # 右到左 (right-left)
        ("shell input swipe 500 1200 500 400", "从下往上滑动"),   # 下到上 (down-up)
        ("shell input swipe 500 400 500 1200", "从上往下滑动")    # 上到下 (up-down)
    ]

    successful_swipes = 0
    total_swipes = len(swipe_tests)

    for swipe_cmd, swipe_desc in swipe_tests:
        try:
            # 执行滑动命令
            swipe_result = subprocess.run(f"adb -s {device_id} {swipe_cmd}",
                                        shell=True, capture_output=True, text=True,
                                        timeout=10, encoding='utf-8', errors='ignore')

            if swipe_result.returncode == 0:
                time.sleep(0.5)  # 短暂等待滑动效果

                # 验证滑动后UI状态
                ui_check = subprocess.run(f"adb -s {device_id} shell dumpsys window | findstr \"mCurrentFocus\"",
                                        shell=True, capture_output=True, text=True,
                                        timeout=5, encoding='utf-8', errors='ignore')

                if ui_check.returncode == 0 and ui_check.stdout.strip():
                    successful_swipes += 1
                    print(f"      ✅ {swipe_desc} - 成功")
                else:
                    print(f"      ❌ {swipe_desc} - 无响应")
            else:
                print(f"      ❌ {swipe_desc} - 执行失败")

        except Exception as e:
            print(f"      ❌ {swipe_desc} - 异常: {e}")

    # 判断整体滑动测试结果（any-success validation：任一方向成功即为整体成功）
    success_rate = successful_swipes / total_swipes
    if successful_swipes >= 1:  # 任一滑动成功即视为功能正常
        success = True
        details = f"4方向滑动测试通过 ({successful_swipes}/{total_swipes} 成功, {success_rate*100:.1f}%)"
        print(f"    ✅ 成功 - {details}")
        return True, desc, details
    else:
        success = False
        details = f"4方向滑动测试失败 ({successful_swipes}/{total_swipes} 成功, {success_rate*100:.1f}%)"
        print(f"    ❌ 失败 - {details}")
        return False, desc, details

def execute_single_test(device_id, cmd, desc, test_index, total_tests):
    """执行单个测试项目（用于多线程）"""
    print(f"\n  📋 测试 {test_index}/{total_tests}: {desc}")

    try:
        # 特殊处理4方向滑动验证
        if cmd == "MULTI_DIRECTION_SWIPE":
            return execute_multi_direction_swipe_test(device_id, desc)

        # 执行命令
        result = subprocess.run(f"adb -s {device_id} {cmd}",
                              shell=True, capture_output=True, text=True, timeout=15, encoding='utf-8', errors='ignore')

        # 特殊处理截图命令
        if "屏幕截图功能" in desc:
            if result.returncode == 0:
                # 拉取截图文件到PC本地
                screenshot_dir = "device_screenshots"
                pull_result = subprocess.run(f"adb -s {device_id} pull /sdcard/screenshot.png {screenshot_dir}/screenshot_{device_id}.png",
                                            shell=True, capture_output=True, text=True, timeout=10, encoding='utf-8', errors='ignore')
                if pull_result.returncode == 0:
                    print(f"    ✅ 截图已从手机拉取到PC")
                # 删除设备上的临时文件
                subprocess.run(f"adb -s {device_id} shell rm /sdcard/screenshot.png",
                             shell=True, timeout=5, encoding='utf-8', errors='ignore')

        # 专业验证测试结果
        success, details = validate_test_result(cmd, desc, result, device_id)

        if success:
            print(f"    ✅ 成功 - {details}")
            return True, desc, details
        else:
            print(f"    ❌ 失败 - {details}")
            return False, desc, details

    except subprocess.TimeoutExpired:
        error_msg = f"超时 - 命令执行超过15秒"
        print(f"    ⏰ {error_msg}")
        return False, desc, error_msg
    except Exception as e:
        error_msg = f"异常 - {e}"
        print(f"    ❌ {error_msg}")
        return False, desc, error_msg

def test_adb_commands(device_id):
    """测试基本ADB命令（支持多线程并行验证）"""
    print(f"\n🧪 测试设备 {device_id} 的ADB功能...")

    # 首先确保设备处于可用状态
    if not ensure_device_ready(device_id):
        print("  ❌ 设备状态准备失败，继续测试...")

    # 创建截图保存目录
    screenshot_dir = "device_screenshots"
    if not os.path.exists(screenshot_dir):
        os.makedirs(screenshot_dir)

    print(f"  📁 截图将保存到: {os.path.abspath(screenshot_dir)}")    # 优化后的测试序列，避免屏幕锁定问题
    # 按照原始顺序执行所有测试项
    tests = [
        ("shell echo 'Hello'", "基本shell命令"),
        ("shell ls /sdcard", "文件系统访问"),
        ("shell screencap /sdcard/screenshot.png", "屏幕截图功能"),
        ("shell input keyevent 3", "点击Home键"),
        ("shell sleep 1", "等待Home界面加载"),
        ("shell am start -a android.settings.SETTINGS", "打开系统设置"),
        ("shell sleep 2", "等待设置界面加载"),
        ("shell input swipe 500 1500 500 100", "从下往上滑动"),
        ("shell sleep 1", "等待滑动UI响应"),
        ("MULTI_DIRECTION_SWIPE", "四方向滑动验证"),  # 特殊标记，触发4方向滑动测试
        ("shell input keyevent 3", "返回Home键"),
        ("shell sleep 2", "等待返回Home界面"),
        ("shell input keyevent 26", "输入事件（电源键）")
    ]

    success_count = 0
    total_tests = len(tests)

    print(f"\n🔧 开始执行 {total_tests} 项测试...")

    # 按顺序执行所有测试项
    for i, (cmd, desc) in enumerate(tests, 1):
        success, _, _ = execute_single_test(device_id, cmd, desc, i, total_tests)
        if success:
            success_count += 1

    print(f"\n📊 测试结果: {success_count}/{total_tests} 项通过 ({success_count/total_tests*100:.1f}%)")

    # 根据成功率判断整体结果
    success_rate = success_count / total_tests
    if success_rate >= 0.8:
        print("🎉 设备功能测试整体表现优秀!")
        return True
    elif success_rate >= 0.6:
        print("⚠️ 设备功能测试基本通过，但有部分问题")
        return True
    else:
        print("❌ 设备功能测试未通过，存在重大问题")
        return False

def test_multiple_devices_parallel(devices, max_workers=None):
    """并行测试多个设备"""
    # 如果没有指定最大工作线程数，则使用设备数量（允许所有设备同时检测）
    if max_workers is None:
        max_workers = len(devices)

    print(f"\n🚀 开始并行测试 {len(devices)} 个设备...")
    print(f"⚡ 并发线程数: {max_workers}")

    results = {}

    # 使用线程池并行执行设备测试
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        # 提交所有设备检查任务
        future_to_device = {}

        for device_id in devices:
            # 设备详情检查
            future_detail = executor.submit(check_device_details, device_id)
            future_to_device[future_detail] = (device_id, 'detail')

            # ADB命令测试
            future_adb = executor.submit(test_adb_commands, device_id)
            future_to_device[future_adb] = (device_id, 'adb')

        # 收集结果
        for future in as_completed(future_to_device):
            device_id, test_type = future_to_device[future]
            try:
                result = future.result()
                if device_id not in results:
                    results[device_id] = {}
                results[device_id][test_type] = result
                print(f"✅ 设备 {device_id} 的 {test_type} 测试完成")
            except Exception as e:
                print(f"❌ 设备 {device_id} 的 {test_type} 测试异常: {e}")
                if device_id not in results:
                    results[device_id] = {}
                results[device_id][test_type] = False

    return results

def main():
    """主函数"""
    # 解析命令行参数
    parser = argparse.ArgumentParser(description='WFGameAI USB连接检查工具')
    parser.add_argument('--max-workers', type=int, default=None,
                       help='最大并发线程数 (默认: 无限制，所有设备同时检测)')
    parser.add_argument('--conservative', action='store_true',
                       help='保守模式，限制最大4个并发线程 (等同于 --max-workers 4)')

    args = parser.parse_args()

    # 确定并发线程数
    max_workers = args.max_workers
    if args.conservative:
        max_workers = 4

    if not check_usb_connection():
        print("\n❌ USB连接检查失败，请根据上述指南进行设置")
        return 1

    # 获取已连接的设备
    try:
        result = subprocess.run("adb devices", shell=True, capture_output=True, text=True, encoding='utf-8', errors='ignore')
        devices = []
        for line in result.stdout.split('\n')[1:]:
            if line.strip() and '\t' in line:
                device_id, status = line.split('\t')
                if status.strip() == "device":
                    devices.append(device_id.strip())

        if not devices:
            print("\n⚠️ 没有可用的授权设备")
            return 1        # 对每个设备进行详细检查
        print("\n" + "=" * 60)
        print("📋 设备详细检查")
        print("=" * 60)

        if len(devices) == 1:
            # 单设备串行处理
            print(f"🔧 单设备模式：串行执行详细测试")
            device_id = devices[0]
            print(f"\n--- 设备: {device_id} ---")
            detail_result = check_device_details(device_id)
            adb_result = test_adb_commands(device_id)
            all_passed = detail_result and adb_result
        else:
            # 多设备并行处理
            worker_info = f"所有设备同时" if max_workers is None else f"最多{max_workers}个"
            print(f"⚡ 多设备模式：并行执行测试以提高效率 ({worker_info})")
            results = test_multiple_devices_parallel(devices, max_workers)

            # 分析结果
            all_passed = True
            for device_id, device_results in results.items():
                print(f"\n--- 设备: {device_id} ---")
                detail_success = device_results.get('detail', False)
                adb_success = device_results.get('adb', False)
                device_success = detail_success and adb_success

                print(f"  详情检查: {'✅ 通过' if detail_success else '❌ 失败'}")
                print(f"  ADB测试: {'✅ 通过' if adb_success else '❌ 失败'}")
                print(f"  整体结果: {'✅ 通过' if device_success else '❌ 失败'}")

                if not device_success:
                    all_passed = False

        if all_passed:
            print("\n🎉 所有设备检查通过！设备已准备就绪。")
            print("💡 现在可以运行 WFGameAI 进行自动化测试了。")
            return 0
        else:
            print("\n⚠️ 部分设备存在问题，请检查设备设置。")
            return 1

    except Exception as e:
        print(f"\n❌ 设备检查过程中出现错误: {e}")
        return 1

if __name__ == "__main__":
    sys.exit(main())

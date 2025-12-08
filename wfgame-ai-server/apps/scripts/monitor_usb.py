import sys
import os
import time
import logging
import django
import threading

# --- 1. 手动设置 Django 环境 ---
# 将项目根目录添加到 sys.path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

# 设置 Django settings 模块
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'wfgame_ai_server_main.settings')

# 初始化 Django
django.setup()
# --- 结束设置 ---

# 导入 Django 模型 (在 django.setup() 之后)
from apps.devices.services import scan
from apps.notifications.services import send_message, SSEEvent

# --- 防抖设置 ---
DEBOUNCE_SECONDS = 1  # 防抖时间（秒）
debounce_timer = None

def _execute_scan():
    """实际执行扫描和通知的函数"""
    logging.info("---------------------------------")
    logging.info(" 🚨 执行设备扫描 & 发送更新通知 ")
    try:
        scan()
        # 发送 SSE 通知
        send_message(None, SSEEvent.DEVICE_UPDATE.value)
    except Exception as e:
        logging.error(f"执行扫描时出错: {e}")
    finally:
        logging.info("---------------------------------")

def scan_device():
    """带防抖功能的设备扫描触发函数"""
    global debounce_timer
    # 如果已有计时器在等待，则取消它
    if debounce_timer and debounce_timer.is_alive():
        debounce_timer.cancel()
        logging.info("已取消之前的扫描计划。")
    
    # 设置一个新的计时器，在指定秒数后执行扫描
    debounce_timer = threading.Timer(DEBOUNCE_SECONDS, _execute_scan)
    debounce_timer.start()
    logging.info(f"设备扫描已加入计划，将在 {DEBOUNCE_SECONDS} 秒后执行...")


# 尝试导入 pywin32 相关的库
try:
    import win32gui
    import win32con
    import win32api
    import win32gui_struct
    import pywintypes
except ImportError:
    print("错误: pywin32 库未安装。请运行 'pip install pywin32' 进行安装。")
    sys.exit(1)


def setup_logging():
    """配置日志记录"""
    log_dir = os.path.join(project_root, 'logs')
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, 'usb_monitor.log')

    # 创建一个格式化器
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')

    # 获取根记录器
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)

    # 如果已经有处理器，则先移除，防止重复记录
    if logger.hasHandlers():
        logger.handlers.clear()

    # 创建文件处理器
    file_handler = logging.FileHandler(log_file, encoding='utf-8')
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    # 创建控制台处理器
    stream_handler = logging.StreamHandler(stream=sys.stdout)
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)


def on_device_change(hwnd, msg, wparam, lparam):
    """
    窗口消息处理函数。
    当硬件设备状态发生改变时，由 Windows 系统回调。
    """
    try:
        dev_info = win32gui_struct.UnpackDEV_BROADCAST(lparam)
    except Exception:
        # 无法解析 lparam，可能不是我们关心的消息，直接返回
        return 1

    # --- 设备插入事件 ---
    if wparam == win32con.DBT_DEVICEARRIVAL:
        if dev_info.devicetype == win32con.DBT_DEVTYP_DEVICEINTERFACE:
            # 新增：判断是哪个接口触发了事件
            # ADB 接口的 GUID
            GUID_ADB = "{F72FE0D4-CBCB-407D-8814-9ED673D0DD6B}"
            # dev_info.guid 是一个 IID 对象，需要转为字符串进行比较
            guid_str = str(getattr(dev_info, 'classguid')).upper()

            if guid_str == GUID_ADB:
                logging.info("✅ ADB 接口已授权/激活，重新扫描设备...")
                scan_device()
            else:
                # 通用USB设备接口
                device_path = dev_info.name
                
                vendor_id = "N/A"
                product_id = "N/A"
                if 'VID_' in device_path:
                    vendor_id = device_path.split('VID_')[1].split('&')[0]
                if 'PID_' in device_path:
                    product_id = device_path.split('PID_')[1].split('&')[0]

                logging.info(f"🔌 设备接口插入: VID={vendor_id}, PID={product_id}")
                # scan_device()

        # 2. 新增：逻辑卷挂载 (例如 "文件传输" 模式或 U盘)
        elif dev_info.devicetype == win32con.DBT_DEVTYP_VOLUME:
            logging.info("逻辑卷挂载: 检测到 '文件传输' 模式或新的存储设备。")
            # dev_info.unitmask 是一个位掩码，表示哪个驱动器号被添加
            # 我们可以遍历它来找出具体的盘符
            for i in range(26):
                if (dev_info.unitmask >> i) & 1:
                    drive_letter = chr(ord('A') + i)
                    logging.info(f"  -> 新驱动器号: {drive_letter}:\\")
            
            # --- Django 业务逻辑 ---
            # 这里可以更新设备状态为“文件传输已就绪”

    # --- 设备拔出事件 ---
    elif wparam == win32con.DBT_DEVICEREMOVECOMPLETE:
        # 1. 通用 USB 设备接口拔出
        if dev_info.devicetype == win32con.DBT_DEVTYP_DEVICEINTERFACE:
            device_path = dev_info.name
            
            vendor_id = "N/A"
            product_id = "N/A"
            if 'VID_' in device_path:
                vendor_id = device_path.split('VID_')[1].split('&')[0]
            if 'PID_' in device_path:
                product_id = device_path.split('PID_')[1].split('&')[0]

            logging.info(f"设备接口拔出: VID={vendor_id}, PID={product_id}")

            # --- Django 业务逻辑 ---
            scan_device()

        # 2. 新增：逻辑卷卸载
        elif dev_info.devicetype == win32con.DBT_DEVTYP_VOLUME:
            logging.info("逻辑卷卸载: '文件传输' 模式关闭或存储设备被拔出。")


    return 1


def main():
    """主函数，启动监听器"""
    setup_logging()
    logging.info("正在初始化USB监听服务...")

    # 注册窗口类
    wc = win32gui.WNDCLASS()
    wc.lpfnWndProc = on_device_change
    wc.lpszClassName = 'StandaloneUSBListener'
    wc.hInstance = win32api.GetModuleHandle(None)
    
    try:
        class_atom = win32gui.RegisterClass(wc)
    except win32gui.error as e:
        if e.winerror == 1410: # ERROR_CLASS_ALREADY_EXISTS
            win32gui.UnregisterClass(wc.lpszClassName, wc.hInstance)
            class_atom = win32gui.RegisterClass(wc)
        else:
            raise

    # 创建一个仅用于消息处理的窗口
    hwnd = win32gui.CreateWindow(
        class_atom,
        'Standalone USB Listener',
        0, 0, 0, 0, 0,
        win32con.HWND_MESSAGE,
        0, 
        wc.hInstance, 
        None
    )

    if not hwnd:
        logging.error("无法创建消息窗口。")
        return

    # --- 关键步骤：注册设备通知 ---
    # 1. 注册通用USB设备接口通知
    GUID_DEVINTERFACE_USB_DEVICE = "{A5DCBF10-6530-11D2-901F-00C04FB951ED}"
    dev_interface_guid_usb = pywintypes.IID(GUID_DEVINTERFACE_USB_DEVICE)
    filter_structure_usb = win32gui_struct.PackDEV_BROADCAST_DEVICEINTERFACE(dev_interface_guid_usb)
    hNotify_usb = win32gui.RegisterDeviceNotification(
        hwnd,
        filter_structure_usb,
        win32con.DEVICE_NOTIFY_WINDOW_HANDLE
    )

    # 2. 新增：注册ADB设备接口通知
    GUID_DEVINTERFACE_ADB = "{F72FE0D4-CBCB-407D-8814-9ED673D0DD6B}"
    dev_interface_guid_adb = pywintypes.IID(GUID_DEVINTERFACE_ADB)
    filter_structure_adb = win32gui_struct.PackDEV_BROADCAST_DEVICEINTERFACE(dev_interface_guid_adb)
    hNotify_adb = win32gui.RegisterDeviceNotification(
        hwnd,
        filter_structure_adb,
        win32con.DEVICE_NOTIFY_WINDOW_HANDLE
    )
    
    logging.info("服务已启动。正在监听USB及ADB设备事件... (按 Ctrl+C 退出)")

    try:
        # 启动服务后扫描设备
        scan_device()
        while True:
            win32gui.PumpWaitingMessages()
            time.sleep(0.1)
    except KeyboardInterrupt:
        logging.info("正在停止服务...")
    finally:
        # 清理所有通知句柄
        if hNotify_usb:
            win32gui.UnregisterDeviceNotification(hNotify_usb)
        if hNotify_adb:
            win32gui.UnregisterDeviceNotification(hNotify_adb)
        win32gui.DestroyWindow(hwnd)
        win32gui.UnregisterClass(class_atom, wc.hInstance)
        logging.info("服务已成功停止。")


if __name__ == "__main__":
    main()
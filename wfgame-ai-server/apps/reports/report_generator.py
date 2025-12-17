#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
报告生成器 - 统一报告生成逻辑
Author: WFGameAI Team
Date: 2025-06-17
"""

import os
import json
import shutil
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional

# 尝试相对导入，如果失败则使用绝对导入
try:
    from .report_manager import ReportManager
    from .report_config import get_report_config
except ImportError:
    from report_manager import ReportManager
    from report_config import get_report_config

# 导入Jinja2模板引擎
try:
    from jinja2 import Template, Environment, FileSystemLoader
except ImportError:
    raise ImportError("❌ Jinja2未安装！请安装Jinja2库: pip install Jinja2")

def find_template_path(template_name: str, report_manager=None) -> Optional[Path]:
    """查找模板文件实际路径"""
    current_file = Path(__file__)

    # 🔧 增强修复：尝试更多可能的模板路径
    candidates = []

    # 1. 如果提供了report_manager，尝试使用其配置
    if report_manager:
        try:
            template_dir = report_manager.config.config.get('devices_report_paths', 'template_dir', fallback='')
            if template_dir:
                candidates.append(Path(template_dir) / template_name)
        except Exception as e:
            print(f"⚠️ 从report_manager获取模板路径失败: {e}")

    # 2. 尝试从配置文件直接获取
    try:
        from .report_config import get_report_config
        config = get_report_config()
        template_dir = config.config.get('devices_report_paths', 'template_dir', fallback='')
        if template_dir:
            candidates.append(Path(template_dir) / template_name)
    except Exception as e:
        print(f"⚠️ 从配置文件获取模板路径失败: {e}")

    # 3. 尝试常见的相对路径
    candidates.extend([
        # 相对于当前文件
        current_file.parent / "templates" / template_name,
        current_file.parent.parent / "templates" / template_name,
        # 相对于项目根目录
        current_file.parent.parent.parent / "staticfiles" / "reports" / "templates" / template_name,
        # 绝对路径
        Path("staticfiles") / "reports" / "templates" / template_name,
    ])

    # 尝试所有候选路径
    for candidate in candidates:
        if candidate.exists():
            print(f"✅ 找到模板文件: {candidate}")
            return candidate

    # 如果所有候选路径都失败，记录详细信息并返回None
    print(f"❌ 未找到模板文件 {template_name}，已尝试以下路径:")
    for candidate in candidates:
        print(f"  - {candidate} {'(存在)' if candidate.exists() else '(不存在)'}")

    return None

class ReportGenerator:
    """报告生成器"""

    # 移除类变量，避免重用旧的汇总报告
    # summary_report_generated = False

    def __init__(self, report_manager: ReportManager):
        """初始化报告生成器"""
        self.report_manager = report_manager
        self.config = report_manager.config

    def generate_device_report(self, device_dir: Path, scripts: List[Dict]) -> bool:
        """
        生成设备报告 - 兼容性方法
        Args:
            device_dir: 设备报告目录
            scripts: 执行的脚本列表
        Returns:
            是否生成成功
        """
        try:
            # 导入必要模块
            from pathlib import Path
            import os

            print(f"📝 开始生成设备报告: {device_dir.name}")

            # 🔧 增强修复：确保device_dir是正确的设备专属目录
            if not isinstance(device_dir, Path):
                device_dir = Path(device_dir)

            # 尝试修正设备目录路径到标准位置
            try:
                if hasattr(self, 'report_manager') and self.report_manager:
                    correct_base_dir = self.report_manager.single_device_reports_dir
                    # 检查当前路径是否已经是标准路径的一部分
                    if str(correct_base_dir) not in str(device_dir.absolute()):
                        print(f"⚠️ 设备目录不在标准位置: {device_dir}")
                        # 使用标准位置
                        target_dir = correct_base_dir / device_dir.name
                        print(f"🔧 切换到标准设备目录: {target_dir}")

                        # 确保目标目录存在
                        if not target_dir.exists():
                            target_dir.mkdir(parents=True, exist_ok=True)

                        device_dir = target_dir
            except Exception as e:
                print(f"⚠️ 尝试修正设备目录失败: {e}")

            # 确保设备目录存在
            if not device_dir.exists():
                print(f"❌ 设备目录不存在: {device_dir}")
                return False

            # 检查设备目录是否包含log.txt文件，如果不存在则创建空文件
            log_txt_path = device_dir / "log.txt"
            if not log_txt_path.exists():
                print(f"⚠️ log.txt不存在，创建空文件: {log_txt_path}")
                with open(log_txt_path, 'w', encoding='utf-8') as f:
                    f.write("# WFGameAI 设备日志文件\n")
                    f.write(f"# 设备: {device_dir.name}\n")
                    f.write(f"# 创建时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")

            # 1. 生成HTML报告 (log.html)
            html_file = self.generate_device_html_report(device_dir.name, device_dir)
            if not html_file:
                print(f"❌ 设备HTML报告生成失败")
                return False

            # 不再复制静态资源到设备目录，使用相对路径引用
            # 静态资源保持在统一位置，减少冗余并保证一致性
            # print(f"📌 使用相对路径引用静态资源，无需复制资源到设备目录")

            print(f"✅ 设备 {device_dir.name} 单设备报告(log.html)生成成功")
            return True

        except Exception as e:
            print(f"❌ 生成设备报告失败: {e}")
            import traceback
            traceback.print_exc()
            return False

    

    def generate_device_html_report(self, device_name: str, device_dir: Path) -> Optional[Path]:
        """
        为指定设备生成HTML报告
        Args:
            device_name: 设备名称
            device_dir: 设备报告目录
        Returns:
            生成的HTML文件路径，失败返回None
        """
        try:
            # 导入必要的模块
            from pathlib import Path
            import os

            print(f"📝 开始生成设备HTML报告: {device_name}")

            # 🔧 增强修复：确保device_dir是正确的设备专属目录
            if not isinstance(device_dir, Path):
                device_dir = Path(device_dir)

            # 检查设备目录是否在正确的位置
            if "WFGameAI.air/log" not in str(device_dir):
                print(f"⚠️ 设备目录不在正确的位置: {device_dir}")
                # 尝试找到正确的设备目录
                try:
                    # 尝试查找正确的设备目录
                    correct_base_dir = self.report_manager.single_device_reports_dir
                    if device_dir.name in os.listdir(correct_base_dir):
                        device_dir = correct_base_dir / device_dir.name
                        print(f"🔧 修正设备目录: {device_dir}")
                except Exception as e:
                    print(f"⚠️ 尝试修正设备目录失败: {e}")


            # 2. 准备报告数据
            report_data = {
                "steps": steps,
                "name": str(device_dir),
                "scale": 0.5,
                "test_result": True,
                "run_end": datetime.now().timestamp(),
                "run_start": datetime.now().timestamp() - 60,
                "static_root": "/static/reports/static/",  # 使用Web相对路径
                "lang": "en",
                "records": [],
                "info": {
                    "name": "script.py",
                    "path": str(device_dir / "script.py"),
                    "author": "",
                    "title": device_name,
                    "desc": "",
                    "devices": {}
                },
                "log": "log.txt",
                "console": ""
            }


            # 4. 保存HTML文件
            html_file = device_dir / "log.html"
            with open(html_file, 'w', encoding='utf-8') as f:
                f.write(html_content)

            print(f"✅ 设备 {device_name} 单设备HTML报告(log.html)生成成功: {html_file}")
            return html_file

        except Exception as e:
            print(f"❌ 生成设备HTML报告失败: {e}")
            import traceback
            traceback.print_exc()
            return None

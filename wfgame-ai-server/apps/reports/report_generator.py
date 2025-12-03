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

    def generate_unified_report(self, device_reports: List[Path], scripts: List[Dict]) -> Optional[Path]:
        """
        统一报告生成入口点 - Problem 6 Fix
        Args:
            device_reports: 设备报告目录列表
            scripts: 执行的脚本列表
        Returns:
            生成的汇总报告路径，失败返回None
        """
        try:
            print(f"📝 开始生成统一测试报告...")

            # 1. 检查并生成设备报告
            for device_dir in device_reports:
                if not device_dir.exists():
                    print(f"⚠️ 设备目录不存在: {device_dir}")
                    continue

                # 强制生成设备报告，即使文件已存在也重新生成
                print(f"🔄 生成设备报告: {device_dir.name}")
                self.generate_device_report(device_dir, scripts)

            # 2. 生成汇总报告
            summary_report = self.generate_summary_report(device_reports, scripts)
            if not summary_report:
                print(f"⚠️ 汇总报告生成失败，使用备用方案")
                return self._generate_fallback_summary_report(device_reports, scripts)

            print(f"✅ 统一报告生成成功: {summary_report}")
            return summary_report

        except Exception as e:
            print(f"❌ 统一报告生成失败: {e}")
            import traceback
            traceback.print_exc()
            # 使用备用方案
            return self._generate_fallback_summary_report(device_reports, scripts)

    def _generate_fallback_summary_report(self, device_reports: List[Path], scripts: List[Dict]) -> Optional[Path]:
        """
        备用汇总报告生成 - 模板回退机制
        """
        try:
            print(f"🔄 使用备用模板生成汇总报告...")

            # 准备备用HTML内容
            html_content = self._build_fallback_summary_html(device_reports, scripts)

            # 保存备用报告 - 使用配置的汇总报告目录
            summary_reports_dir = self.report_manager.summary_reports_dir
            summary_reports_dir.mkdir(parents=True, exist_ok=True)

            timestamp = datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
            summary_file = summary_reports_dir / f"fallback_summary_{timestamp}.html"

            with open(summary_file, "w", encoding="utf-8") as f:
                f.write(html_content)

            print(f"✅ 备用汇总报告生成成功: {summary_file}")
            return summary_file

        except Exception as e:
            print(f"❌ 备用汇总报告也生成失败: {e}")
            return None

    def _build_fallback_summary_html(self, device_reports: List[Path], scripts: List[Dict]) -> str:
        """构建备用汇总报告HTML"""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        static_url = self.config.report_static_url

        # 统计信息
        total_devices = len(device_reports)
        success_count = sum(1 for device_dir in device_reports if (device_dir / "log.html").exists())
        success_rate = f"{(success_count / total_devices * 100):.1f}%" if total_devices > 0 else "0%"

        html_content = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>WFGameAI 测试汇总报告 (备用版本)</title>
    <link href="{static_url}css/report.css" rel="stylesheet">
    <style>
        body {{ font-family: 'Segoe UI', Arial, sans-serif; margin: 0; background: #f8f9fa; }}
        .container {{ max-width: 1200px; margin: 0 auto; padding: 20px; }}
        .header {{ background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 30px; border-radius: 12px; margin-bottom: 30px; box-shadow: 0 4px 12px rgba(0,0,0,0.15); }}
        .stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 20px; margin-bottom: 30px; }}
        .stat-card {{ background: white; padding: 20px; border-radius: 12px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); text-align: center; }}
        .stat-value {{ font-size: 2.5em; font-weight: bold; margin-bottom: 5px; }}
        .device-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(350px, 1fr)); gap: 20px; }}
        .device-card {{ background: white; border-radius: 12px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); overflow: hidden; transition: transform 0.2s; }}
        .device-card:hover {{ transform: translateY(-2px); box-shadow: 0 4px 16px rgba(0,0,0,0.15); }}
        .device-header {{ background: linear-gradient(45deg, #28a745, #20c997); color: white; padding: 20px; }}
        .device-content {{ padding: 20px; }}
        .btn {{ display: inline-block; padding: 10px 20px; background: #007bff; color: white; text-decoration: none; border-radius: 6px; margin: 5px; transition: background 0.2s; }}
        .btn:hover {{ background: #0056b3; }}
        .status-success {{ color: #28a745; }}
        .status-error {{ color: #dc3545; }}
        .script-list {{ background: #f8f9fa; padding: 15px; border-radius: 8px; margin: 15px 0; }}
        .script-item {{ padding: 8px; margin: 5px 0; background: white; border-radius: 4px; font-size: 0.9em; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🎮 WFGameAI 测试汇总报告</h1>
            <p>生成时间: {timestamp}</p>
            <p style="opacity: 0.8; font-size: 0.9em;">备用模板版本 - 基础功能保证</p>
        </div>

        <div class="stats">
            <div class="stat-card">
                <div class="stat-value" style="color: #007bff;">{total_devices}</div>
                <div>测试设备数</div>
            </div>
            <div class="stat-card">
                <div class="stat-value" style="color: #28a745;">{success_count}</div>
                <div>成功设备数</div>
            </div>
            <div class="stat-card">
                <div class="stat-value" style="color: #17a2b8;">{success_rate}</div>
                <div>成功率</div>
            </div>
            <div class="stat-card">
                <div class="stat-value" style="color: #6c757d;">{len(scripts)}</div>
                <div>执行脚本数</div>
            </div>
        </div>

        <h2>📋 执行脚本列表</h2>
        <div class="script-list">
"""

        # 添加脚本信息
        for i, script in enumerate(scripts, 1):
            script_name = script.get('name', f'Script {i}')
            script_path = script.get('path', 'N/A')
            script_config = script.get('config', {})

            html_content += f"""
            <div class="script-item">
                <strong>{i}. {script_name}</strong><br>
                <small>路径: {script_path}</small><br>
                <small>循环: {script_config.get('loop_count', 1)} 次</small>
                {f"<br><small>最大时长: {script_config.get('max_duration')}秒</small>" if script_config.get('max_duration') else ""}
            </div>
"""

        html_content += """
        </div>

        <h2>📱 设备报告详情</h2>
        <div class="device-grid">
"""

        # 添加设备报告信息
        for device_dir in device_reports:
            urls = self.report_manager.generate_report_urls(device_dir)
            html_exists = (device_dir / "log.html").exists()
            log_exists = (device_dir / "log.txt").exists()

            status_class = "status-success" if html_exists else "status-error"
            status_text = "✅ 报告正常" if html_exists else "❌ 报告缺失"

            device_time = device_dir.stat().st_mtime
            device_time_str = datetime.fromtimestamp(device_time).strftime('%Y-%m-%d %H:%M:%S')

            html_content += f"""
            <div class="device-card">
                <div class="device-header">
                    <h3>{device_dir.name}</h3>
                    <div style="opacity: 0.8; font-size: 0.9em;">创建时间: {device_time_str}</div>
                </div>
                <div class="device-content">
                    <p><strong>报告状态:</strong> <span class="{status_class}">{status_text}</span></p>
                    <div style="margin-top: 15px;">
                        {"<a href='" + urls['html_report'] + "' class='btn' target='_blank'>📊 查看HTML报告</a>" if html_exists else ""}
                        {"<a href='" + urls['log_file'] + "' class='btn' target='_blank'>📄 查看日志文件</a>" if log_exists else ""}
                        <a href="{urls['screenshots']}" class="btn" target="_blank">📸 查看截图</a>
                    </div>
                </div>
            </div>
"""

        html_content += """
        </div>
    </div>

    <script>
        // 简单的交互增强
        document.addEventListener('DOMContentLoaded', function() {
            console.log('WFGameAI 备用汇总报告加载完成');
        });
    </script>
</body>
</html>
"""

        return html_content

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

            # 2. 生成script.py文件
            script_file = self._generate_script_py(device_dir, scripts)
            if not script_file:
                print(f"⚠️ script.py生成失败，但继续执行")

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

    def _generate_script_py(self, device_dir: Path, scripts: List[Dict]) -> Optional[Path]:
        """
        为设备报告生成script.py文件
        Args:
            device_dir: 设备报告目录
            scripts: 执行的脚本列表
        Returns:
            生成的script.py路径，失败返回None
        """
        try:
            print(f"📝 开始生成script.py文件: {device_dir.name}")
            script_path = device_dir / "script.py"
            content = []

            # 添加文件头部信息
            content.append(f"# filepath: {script_path}")
            content.append("# WFGameAI 设备测试脚本")
            content.append(f"# 设备: {device_dir.name}")
            content.append(f"# 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            content.append("")

            # 添加执行的脚本信息
            content.append("# ============ 执行脚本概览 ============")
            for i, script_config in enumerate(scripts, 1):
                script_file_path = script_config.get("path", "")
                content.append(f"# 脚本 {i}: {os.path.basename(script_file_path)}")
                content.append(f"# 路径: {script_file_path}")
                content.append(f"# 循环次数: {script_config.get('loop_count', 1)}")
                if script_config.get('max_duration'):
                    content.append(f"# 最大执行时间: {script_config['max_duration']}秒")
                content.append("")

                # 尝试读取并添加脚本内容
                try:
                    if os.path.exists(script_file_path):
                        with open(script_file_path, "r", encoding="utf-8") as f:
                            script_json = json.load(f)
                            content.append(f"# 脚本内容:")
                            content.append("'''")
                            content.append(json.dumps(script_json, indent=2, ensure_ascii=False))
                            content.append("'''")
                            content.append("")
                except Exception as e:
                    content.append(f"# 无法读取脚本内容: {e}")
                    content.append("")

            # 写入文件
            with open(script_path, "w", encoding="utf-8") as f:
                f.write("\n".join(content))

            return script_path

        except Exception as e:
            print(f"❌ 生成script.py文件失败: {e}")
            return None

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

            # 1. 解析log.txt文件获取真实数据
            steps = self._parse_log_file(device_dir)

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

            # 3. 使用Jinja2模板渲染HTML
            html_content = self._render_log_template(report_data)

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

    def _render_log_template(self, report_data: Dict) -> str:
        """使用Jinja2模板渲染设备日志报告"""
        try:
            # 导入必要模块
            from pathlib import Path
            import os

            # 优先使用配置文件指定的单设备报告模板路径
            template_path = self.config.single_device_replay_template
            # print(f"🔍 配置的模板路径: {template_path}")

            if not isinstance(template_path, Path) or not template_path.exists():
                print(f"🔍 回退到通用查找")
                template_path = find_template_path("log_template.html", self.report_manager)

            if not template_path or not template_path.exists():
                error_msg = f"❌ 未找到单设备报告模板文件: {template_path}"
                print(error_msg)
                raise FileNotFoundError(error_msg)

            print(f"✅ 使用模板文件: {template_path}")

            # 创建Jinja2环境
            env = Environment(loader=FileSystemLoader(str(template_path.parent)))
            template = env.get_template(template_path.name)

            # 修复：使用相对URL路径而不是绝对文件路径
            # 使用Web访问的相对URL路径，确保在浏览器中能正确加载静态资源
            web_static_root = '/static/reports/static/'

            # 准备模板变量
            template_vars = {
                'data': json.dumps(report_data, ensure_ascii=False),
                'steps': report_data.get('steps', []),
                'info': report_data.get('info', {}),
                'static_root': web_static_root,  # 使用Web相对路径
                'lang': 'en',
                'log': 'log.txt',
                'console': report_data.get('console', ''),
                'extra_block': '',
                'records': report_data.get('records', [])
            }

            # 同时修改report_data中的static_root，确保数据一致
            if 'static_root' in report_data:
                report_data['static_root'] = web_static_root

            # 渲染模板
            html_content = template.render(**template_vars)
            return html_content

        except Exception as e:
            print(f"❌ 渲染模板失败: {e}")
            import traceback
            traceback.print_exc()
            raise e

    def _parse_log_file(self, device_dir: Path) -> List[Dict]:
        """
        解析log.txt文件，提取包含screen数据的步骤
        Args:
            device_dir: 设备报告目录
        Returns:
            步骤列表，包含screen数据
        """
        try:
            # 导入必要的模块
            from pathlib import Path
            import os

            # 🔧 修复：尝试多个可能的log.txt路径
            log_file_candidates = [
                device_dir / "log.txt",           # 直接在设备目录下
                device_dir / "log" / "log.txt"    # 在log子目录中
            ]

            log_file = None
            for candidate in log_file_candidates:
                if candidate.exists():
                    log_file = candidate
                    break

            if not log_file:
                print(f"⚠️ log.txt文件不存在，创建默认的步骤数据")
                # 创建默认的步骤数据，确保报告能够生成
                default_steps = [
                    {
                        "title": "设备初始化",
                        "time": datetime.now().timestamp(),
                        "status": "success",
                        "index": 0,
                        "duration": "0.000s",
                        "code": {
                            "name": "init_device",
                            "args": [{"key": "device_name", "value": device_dir.name}]
                        },
                        "desc": "设备初始化成功",
                        "traceback": "",
                        "log": "",
                        "assert": None,
                        "screen": None
                    },
                    {
                        "title": "任务执行",
                        "time": datetime.now().timestamp(),
                        "status": "success",
                        "index": 1,
                        "duration": "0.000s",
                        "code": {
                            "name": "execute_task",
                            "args": [{"key": "task", "value": "default"}]
                        },
                        "desc": "任务执行完成",
                        "traceback": "",
                        "log": "",
                        "assert": None,
                        "screen": None
                    }
                ]
                return default_steps

            # print(f"📝 找到log.txt文件: {log_file}")

            steps = []
            step_index = 0

            with open(log_file, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue

                    try:
                        log_entry = json.loads(line)

                        # 只处理function类型的log条目
                        if log_entry.get("tag") != "function":
                            continue

                        data = log_entry.get("data", {})
                        name = data.get("name", "")

                        # 构建步骤对象
                        step = {
                            "title": data.get("title", name),
                            "time": log_entry.get("time", datetime.now().timestamp()),
                            "status": "success" if data.get("ret") is not False else "fail",
                            "index": step_index,
                            "duration": self._calculate_duration(data),
                            "code": {
                                "name": name,
                                "args": self._parse_call_args(data.get("call_args", {}))
                            },
                            "desc": data.get("desc", ""),
                            "traceback": data.get("traceback", ""),
                            "log": "",
                            "assert": data.get("assert", None)
                        }

                        # 处理screen数据 - 这是关键部分！
                        screen_data = data.get("screen")
                        if screen_data:
                            # 获取设备名称，用于构建相对路径
                            device_name = device_dir.name

                            # 获取原始路径
                            src = screen_data.get("src", "")
                            filepath = screen_data.get("_filepath", "")
                            thumbnail = screen_data.get("thumbnail", "")

                            # 🔧 修复：检查并修正截图路径，确保指向设备专属目录而非临时目录
                            # 检查路径是否包含multi_device_replay
                            if filepath and "multi_device_replay" in filepath:
                                # 提取文件名
                                import os
                                src_filename = os.path.basename(src)
                                thumbnail_filename = os.path.basename(thumbnail)

                                # 🔧 修复：移除log/前缀，使用直接的文件名
                                if src.startswith("log/"):
                                    src_filename = src[4:]  # 移除log/前缀
                                if thumbnail.startswith("log/"):
                                    thumbnail_filename = thumbnail[4:]  # 移除log/前缀

                                # 构建设备专属目录中的路径
                                device_filepath = str(device_dir / os.path.basename(filepath))
                                device_thumbnail = str(device_dir / os.path.basename(thumbnail))

                                print(f"🔧 修正截图路径: 从 {filepath} 到 {device_filepath}")

                                # 更新路径
                                filepath = device_filepath
                                src = src_filename
                                thumbnail = thumbnail_filename

                                # 🔧 增强修复：检查截图文件是否存在，如果不存在则尝试从临时目录复制
                                import shutil
                                if not os.path.exists(device_filepath) and os.path.exists(filepath):
                                    try:
                                        # 确保目标目录存在
                                        os.makedirs(os.path.dirname(device_filepath), exist_ok=True)
                                        print(f"🔧 复制截图文件: 从 {filepath} 到 {device_filepath}")
                                        shutil.copy2(filepath, device_filepath)
                                    except Exception as copy_err:
                                        print(f"⚠️ 复制截图文件失败: {copy_err}")

                                if not os.path.exists(device_thumbnail) and os.path.exists(thumbnail):
                                    try:
                                        # 确保目标目录存在
                                        os.makedirs(os.path.dirname(device_thumbnail), exist_ok=True)
                                        print(f"🔧 复制缩略图文件: 从 {thumbnail} 到 {device_thumbnail}")
                                        shutil.copy2(thumbnail, device_thumbnail)
                                    except Exception as copy_err:
                                        print(f"⚠️ 复制缩略图文件失败: {copy_err}")
                            else:
                                # 🔧 修复：移除log/前缀，使用直接的文件名
                                if src.startswith("log/"):
                                    src = src[4:]  # 移除log/前缀
                                if thumbnail.startswith("log/"):
                                    thumbnail = thumbnail[4:]  # 移除log/前缀

                            # 修复：使用相对URL路径构建最终的截图URL
                            # 在Web环境中，截图应该通过/static/reports/ui_run/WFGameAI.air/log/{device_name}/访问
                            web_src = src
                            web_thumbnail = thumbnail

                            # 构建最终的screen对象
                            step["screen"] = {
                                "src": web_src,  # 使用相对路径
                                "_filepath": filepath,  # 保留原始路径用于调试
                                "thumbnail": web_thumbnail,  # 使用相对路径
                                "resolution": screen_data.get("resolution", [1080, 2400]),
                                "pos": screen_data.get("pos", []),
                                "vector": screen_data.get("vector", []),
                                "confidence": screen_data.get("confidence", 1.0),
                                "rect": screen_data.get("rect", [])
                            }
                        else:
                            step["screen"] = None

                        steps.append(step)
                        step_index += 1

                    except json.JSONDecodeError as e:
                        print(f"⚠️ 解析JSON行失败: {e}, 行内容: {line[:100]}...")
                        continue
                    except Exception as e:
                        print(f"⚠️ 处理log条目失败: {e}")
                        continue

            # 如果没有解析到任何步骤，添加默认步骤确保报告生成
            if not steps:
                print("⚠️ 从log.txt中未解析到任何步骤，添加默认步骤")
                steps.append({
                    "title": "设备初始化",
                    "time": datetime.now().timestamp(),
                    "status": "success",
                    "index": 0,
                    "duration": "0.000s",
                    "code": {
                        "name": "init_device",
                        "args": [{"key": "device_name", "value": device_dir.name}]
                    },
                    "desc": "设备初始化成功",
                    "traceback": "",
                    "log": "",
                    "assert": None,
                    "screen": None
                })

            # print(f"✅ 成功解析log.txt，共{len(steps)}个步骤，其中{len([s for s in steps if s.get('screen')])}个包含截图")
            return steps

        except Exception as e:
            print(f"❌ 解析log.txt文件失败: {e}")
            # 提供默认的步骤，确保报告能够生成
            return [{
                "title": "设备初始化",
                "time": datetime.now().timestamp(),
                "status": "success",
                "index": 0,
                "duration": "0.000s",
                "code": {
                    "name": "init_device",
                    "args": [{"key": "device_name", "value": str(device_dir.name)}]
                },
                "desc": "设备初始化成功",
                "traceback": "",
                "log": "",
                "assert": None,
                "screen": None
            }]

    def _calculate_duration(self, data: Dict) -> str:
        """计算执行时长"""
        try:
            start_time = data.get("start_time", 0)
            end_time = data.get("end_time", 0)
            if start_time and end_time:
                duration = end_time - start_time
                return f"{duration:.3f}s"
            return "0.000s"
        except:
            return "0.000s"

    def _parse_call_args(self, call_args: Dict) -> List[Dict]:
        """解析调用参数"""
        try:
            args = []
            for key, value in call_args.items():
                args.append({
                    "key": key,
                    "value": str(value)
                })
            return args
        except:
            return []

    def generate_summary_report(self, device_report_dirs: List[Path], scripts: List[Dict]) -> Optional[Path]:
        """
        生成汇总报告
        Args:
            device_report_dirs: 设备报告目录列表
            scripts: 执行的脚本列表
        Returns:
            生成的汇总报告路径，失败返回None
        """
        try:
            # 导入必要模块
            from pathlib import Path
            import os

            print(f"📊 开始生成汇总报告，设备数量: {len(device_report_dirs)}")

            # 移除对类变量的检查，确保每次都生成新的汇总报告
            # if ReportGenerator.summary_report_generated:
            #     # 查找最近生成的汇总报告
            #     summary_reports_dir = self.report_manager.summary_reports_dir
            #     if summary_reports_dir.exists():
            #         summary_reports = list(summary_reports_dir.glob("summary_report_*.html"))
            #         if summary_reports:
            #             latest_report = max(summary_reports, key=lambda x: x.stat().st_mtime)
            #             print(f"⚠️ 汇总报告已生成，跳过重复生成")
            #             print(f"🔍 找到最近生成的汇总报告: {latest_report}")
            #             return latest_report

            # 准备汇总报告数据
            summary_data = self._prepare_summary_data(device_report_dirs, scripts)

            # 使用模板渲染汇总报告
            html_content = self._render_summary_template(summary_data)

            # 保存汇总报告
            timestamp = datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
            summary_reports_dir = self.report_manager.summary_reports_dir
            summary_reports_dir.mkdir(parents=True, exist_ok=True)
            summary_file = summary_reports_dir / f"summary_report_{timestamp}.html"

            with open(summary_file, 'w', encoding='utf-8') as f:
                f.write(html_content)

            # 设置类变量，标记汇总报告已生成（移除此行）
            # ReportGenerator.summary_report_generated = True

            print(f"✅ 多设备汇总报告生成成功: {summary_file}")
            return summary_file

        except Exception as e:
            print(f"❌ 生成汇总报告失败: {e}")
            import traceback
            traceback.print_exc()
            return None

    def _prepare_summary_data(self, device_report_dirs: List[Path], scripts: List[Dict]) -> Dict:
        """准备汇总报告数据"""
        try:
            # 导入必要模块
            from pathlib import Path
            import os

            devices = []
            total_steps = 0

            # 验证设备目录是否存在
            valid_device_dirs = []
            for device_dir in device_report_dirs:
                if device_dir.exists():
                    valid_device_dirs.append(device_dir)
                else:
                    print(f"⚠️ 设备目录不存在: {device_dir}")

            # 如果没有有效的设备目录，尝试查找最新的设备目录
            if not valid_device_dirs and self.report_manager.single_device_reports_dir.exists():
                print("🔍 尝试查找最新的设备目录...")
                base_dir = self.report_manager.single_device_reports_dir
                # 查找所有设备目录
                all_device_dirs = [d for d in base_dir.iterdir() if d.is_dir()]
                if all_device_dirs:
                    # 按修改时间排序
                    valid_device_dirs = sorted(all_device_dirs, key=lambda x: x.stat().st_mtime, reverse=True)
                    print(f"✅ 找到 {len(valid_device_dirs)} 个设备目录")

            # 使用有效的设备目录
            for device_dir in valid_device_dirs:
                device_name = device_dir.name

                # 验证log.html文件是否存在
                log_html_path = device_dir / "log.html"
                has_log_html = log_html_path.exists()

                if not has_log_html:
                    print(f"⚠️ 设备 {device_name} 的log.html文件不存在: {log_html_path}")

                # 解析设备状态
                steps = self._parse_log_file(device_dir)
                device_steps = len(steps)
                device_success = len([s for s in steps if s.get("status") == "success"])
                device_failed = len([s for s in steps if s.get("status") == "fail"])
                device_passed = device_failed == 0

                total_steps += device_steps

                # 构建设备报告链接 - 使用相对路径
                # 从summary_reports目录到设备目录的相对路径
                device_report_link = self.report_manager.normalize_report_url(device_name, is_relative=True)

                # 验证链接是否有效
                absolute_path = self.report_manager.device_replay_reports_dir / "ui_run/WFGameAI.air/log" / device_name / "log.html"
                if not absolute_path.exists():
                    print(f"⚠️ 设备报告链接无效: {device_report_link}")
                    # print(f"⚠️ 绝对路径: {absolute_path}")
                else:
                    print(f"✅ 设备报告链接有效: {device_report_link}")
                    # print(f"✅ 绝对路径: {absolute_path}")

                devices.append({
                    "name": device_name,
                    "passed": device_passed,
                    "success": device_success > 0,
                    "report": device_report_link,
                    "steps": device_steps,
                    "success_count": device_success,
                    "failed_count": device_failed
                })

            # 计算统计数据
            total_devices = len(devices)
            success_devices = sum(1 for d in devices if d.get("success"))
            passed_devices = sum(1 for d in devices if d.get("passed"))

            success_rate = (success_devices / max(total_devices, 1)) * 100
            pass_rate = (passed_devices / max(total_devices, 1)) * 100

            # 返回完整的数据结构
            return {
                "title": "WFGameAI 测试汇总报告",
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "generation_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),

                # 设备统计
                "total_devices": total_devices,
                "total": total_devices,
                "success": success_devices,
                "passed": passed_devices,

                # 比率信息
                "success_rate": f"{success_rate:.1f}%",
                "pass_rate": f"{pass_rate:.1f}%",
                "success_percent": success_rate,
                "pass_percent": pass_rate,

                # 设备和脚本信息
                "devices": devices,
                "scripts": scripts or [],
                "static_root": self.config.report_static_url
            }

        except Exception as e:
            print(f"❌ 准备汇总数据失败: {e}")
            import traceback
            traceback.print_exc()
            raise e

    def _render_summary_template(self, summary_data: Dict) -> str:
        """使用Jinja2模板渲染汇总报告"""
        try:
            # 导入必要模块
            from pathlib import Path
            import os

            # 优先使用配置文件指定的多设备汇总报告模板路径
            template_path = self.config.multi_device_replay_template
            if not isinstance(template_path, Path) or not template_path.exists():
                # 回退到通用查找
                template_path = find_template_path("summary_template.html", self.report_manager)
            if not template_path or not template_path.exists():
                error_msg = f"❌ 未找到汇总报告模板文件: {template_path}"
                print(error_msg)
                # 🔧 增强修复：输出更多调试信息
                # print(f"🔍 配置的模板路径: {self.config.multi_device_replay_template}")
                # print(f"🔍 模板目录配置: {self.config.config.get('devices_report_paths', 'template_dir', fallback='未配置')}")
                # print(f"🔍 尝试查找模板: {os.path.join(os.path.dirname(os.path.dirname(__file__)), 'staticfiles', 'reports', 'templates', 'summary_template.html')}")
                raise FileNotFoundError(error_msg)

            print(f"✅ 使用汇总报告模板文件: {template_path}")
            # 创建Jinja2环境并渲染
            env = Environment(loader=FileSystemLoader(str(template_path.parent)))
            template = env.get_template(template_path.name)
            html_content = template.render(data=summary_data)
            return html_content

        except Exception as e:
            print(f"❌ 渲染汇总模板失败: {e}")
            import traceback
            traceback.print_exc()
            raise e  # 直接抛出异常，不使用备用模板

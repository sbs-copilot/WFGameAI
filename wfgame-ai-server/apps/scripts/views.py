#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================
Description:
脚本管理视图
Author: WFGame AI Team
CreateDate: 2025-05-15
Version: 1.0
===============================
"""

import os
import json
import uuid
import time
import enum
import shutil
import psutil
import logging
import platform
import tempfile
import importlib
import subprocess
import traceback
import sys
import threading
import signal
import re
from pathlib import Path
from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta
from dataclasses import dataclass
from enum import Enum, auto
from threading import Thread, Lock
from concurrent.futures import ThreadPoolExecutor, as_completed
import concurrent.futures

import django
from django.conf import settings
from django.db import transaction
from django.http import JsonResponse, FileResponse, Http404, HttpResponse
from django.utils import timezone, translation
from django.utils.translation import gettext as _
from django.views.decorators.csrf import csrf_exempt
from django.contrib.auth.models import User
from django.shortcuts import get_object_or_404

from rest_framework import status, permissions, viewsets, views
from rest_framework.views import APIView
from rest_framework.parsers import FileUploadParser
from rest_framework.decorators import api_view, action, permission_classes
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny
from apps.core.utils.response import api_response
from django.conf import settings

from .models import (
    Script, ScriptCategory, ScriptVersion, ScriptExecution
)
from .serializers import (
    ScriptSerializer, ScriptCategorySerializer, ScriptVersionSerializer,
    ScriptExecutionSerializer, ScriptCreateSerializer, ScriptUpdateSerializer
)

# 设置一个全局任务管理器
task_manager = None

# 设置日志
logger = logging.getLogger(__name__)

# 设置默认语言为中文
translation.activate('zh-hans')

# 定义全局变量
# 测试用例目录路径
TESTCASE_DIR = os.path.join(settings.BASE_DIR, 'apps', 'scripts', 'testcase')
# 设备报告目录
DEVICE_REPORTS_DIR = os.path.join(settings.BASE_DIR, 'staticfiles', 'reports')
# 确保目录存在
os.makedirs(TESTCASE_DIR, exist_ok=True)
os.makedirs(DEVICE_REPORTS_DIR, exist_ok=True)


# 辅助函数：生成标准格式的API响应
# deprecated, 使用 api_response 替代
def create_api_response(success=True, message="", data=None, status_code=status.HTTP_200_OK):
    """
    创建标准格式的API响应

    Args:
        success: 操作是否成功
        message: 提示信息
        data: 返回的数据
        status_code: HTTP状态码

    Returns:
        Response对象
    """
    response_data = {
        "success": success,
        "message": message
    }

    # 添加数据
    if data is not None:
        if isinstance(data, dict):
            response_data.update(data)
        else:
            response_data["data"] = data

    return Response(response_data, status=status_code)


class UTF8StreamHandler(logging.StreamHandler):
    """UTF-8流处理器，确保日志输出使用UTF-8编码"""

    def __init__(self, stream=None):
        super().__init__(stream)
        self.encoding = 'utf-8'

    def emit(self, record):
        try:
            msg = self.format(record)
            stream = self.stream
            if isinstance(msg, str):
                msg = msg.encode(self.encoding, 'replace').decode(self.encoding)
            stream.write(msg)
            stream.write(self.terminator)
            self.flush()
        except Exception:
            self.handleError(record)


def setup_utf8_logging():
    """配置UTF-8编码的日志输出"""
    root_logger = logging.getLogger()
    for handler in list(root_logger.handlers):
        if isinstance(handler, logging.StreamHandler):
            root_logger.removeHandler(handler)

    # 添加UTF-8流处理器
    utf8_handler = UTF8StreamHandler(sys.stdout)
    formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')
    utf8_handler.setFormatter(formatter)
    root_logger.addHandler(utf8_handler)


# 查找脚本路径的函数
def find_script_path(script_name):
    """
    查找脚本文件的完整路径
    """
    # 脚本目录
    script_dirs = [
        os.path.join(settings.BASE_DIR, 'apps', 'scripts', 'testcase'),
        os.path.join(settings.BASE_DIR, 'apps', 'scripts'),
    ]

    for script_dir in script_dirs:
        script_path = os.path.join(script_dir, script_name)
        if os.path.exists(script_path):
            return script_path

    return None


@api_view(['POST'])
@csrf_exempt
@permission_classes([permissions.AllowAny])
def edit_script(request):
    """
    编辑脚本

    接受JSON格式的脚本数据，更新指定脚本文件
    """
    try:
        # 从请求中获取脚本数据
        script_data = request.data
        logger.info(f"收到编辑脚本请求: {script_data}")
        if not script_data:
            return Response({'success': False, 'message': '请求数据为空'}, status=status.HTTP_400_BAD_REQUEST)

        # 获取操作类型和文件名
        operation = script_data.get('operation', 'write')  # 默认为写入操作
        filename = script_data.get('filename')

        if not filename:
            return Response({'success': False, 'message': '文件名不能为空'}, status=status.HTTP_400_BAD_REQUEST)

        # 文件名清理 - 处理特殊情况
        logger.info(f"原始文件名: '{filename}'")

        # 首先检查文件是否存在
        script_dirs = [
            os.path.join(settings.BASE_DIR, 'apps', 'scripts', 'testcase'),
            os.path.join(settings.BASE_DIR, 'apps', 'scripts'),
        ]

        script_path = None

        # 直接尝试查找完全匹配的文件
        for script_dir in script_dirs:
            temp_path = os.path.join(script_dir, filename)
            logger.info(f"检查文件路径: '{temp_path}'")
            if os.path.exists(temp_path):
                script_path = temp_path
                logger.info(f"找到文件: '{script_path}'")
                break

        # 如果没有找到完全匹配，尝试处理数字前缀的情况
        if script_path is None:
            # 提取文件名的核心部分（去除数字前缀）
            file_parts = filename.split('_', 1)
            core_filename = None

            # 检查是否是类似 "card2_game_function_20250723_190349.json" 格式
            if len(file_parts) > 1:
                # 如果第一部分不是数字，可能是 "card2_game_function_..." 格式
                if not file_parts[0].isdigit():
                    core_filename = filename
                    logger.info(f"检测到核心文件名(无数字前缀): '{core_filename}'")

            # 如果找到了核心文件名，尝试查找带数字前缀的匹配文件
            if core_filename:
                for script_dir in script_dirs:
                    if os.path.exists(script_dir):
                        # 列出目录中的所有文件
                        for file in os.listdir(script_dir):
                            if file.lower().endswith('.json'):
                                # 检查文件是否是 "数字_核心文件名" 格式
                                if re.match(r'^\d+_', file) and core_filename in file:
                                    temp_path = os.path.join(script_dir, file)
                                    logger.info(f"找到带数字前缀的匹配文件: '{temp_path}'")
                                    script_path = temp_path
                                    break
                    if script_path:
                        break

        # 如果仍然没有找到，尝试模糊匹配
        if script_path is None:
            # 尝试在testcase目录中模糊匹配文件
            testcase_dir = os.path.join(settings.BASE_DIR, 'apps', 'scripts', 'testcase')
            possible_matches = []

            if os.path.exists(testcase_dir):
                for file in os.listdir(testcase_dir):
                    if file.lower().endswith('.json'):
                        # 计算文件名相似度
                        file_lower = file.lower()
                        filename_lower = filename.lower()

                        # 特殊处理数字开头的文件名 (如 5_card2_game...)
                        if re.match(r'^\d+_', file):
                            # 提取数字后的部分
                            file_core = file.split('_', 1)[1] if '_' in file else file

                            # 如果文件名核心部分包含在请求的文件名中，或反之
                            if file_core.lower() in filename_lower or filename_lower in file_core.lower():
                                similarity = 1000 + len(file)  # 给予高优先级
                                logger.info(f"数字前缀文件高匹配度: '{file}' 与 '{filename}'")
                            else:
                                # 标准字符匹配评分
                                similarity = sum(1 for a, b in zip(file_lower, filename_lower) if a == b)
                        else:
                            # 标准字符匹配评分
                            similarity = sum(1 for a, b in zip(file_lower, filename_lower) if a == b)

                        # 如果文件名是另一个的子字符串，增加相似度
                        if filename_lower in file_lower or file_lower in filename_lower:
                            similarity += 50

                        # 特殊处理：如果请求的是不带数字前缀的文件名，但实际文件有数字前缀
                        if not re.match(r'^\d+_', filename) and re.match(r'^\d+_', file):
                            # 比较不带数字前缀的部分
                            file_without_prefix = file.split('_', 1)[1] if '_' in file else file
                            if file_without_prefix.lower() == filename_lower:
                                similarity += 200  # 给予极高优先级
                                logger.error(f"完美匹配(忽略数字前缀): '{file}' 匹配 '{filename}'")

                        possible_matches.append((file, similarity))

            # 按相似度排序并取前5个最匹配的文件
            possible_matches.sort(key=lambda x: x[1], reverse=True)
            top_matches = possible_matches[:5]

            # 如果第一个匹配的相似度特别高，直接使用它
            if top_matches and top_matches[0][1] > 1000:
                best_match = top_matches[0][0]
                script_path = os.path.join(testcase_dir, best_match)
                logger.error(f"使用最佳匹配文件: '{script_path}'")
            else:
                matches_info = ", ".join([f"'{match[0]}'" for match in top_matches]) if top_matches else "无匹配文件"
                logger.error(f"找不到脚本文件: '{filename}'，可能的匹配项: {matches_info}")

                return Response({
                    'success': False,
                    'message': f'找不到脚本文件: {filename}',
                    'possible_matches': [match[0] for match in top_matches]
                }, status=status.HTTP_404_NOT_FOUND)

        # 根据操作类型执行不同的操作
        if operation == 'read':
            # 读取脚本内容
            try:
                with open(script_path, 'r', encoding='utf-8') as f:
                    content = f.read()

                # 解析JSON以获取步骤数
                script_json = json.loads(content)
                steps = script_json.get('steps', [])

                # 获取文件状态
                file_stat = os.stat(script_path)
                created = datetime.fromtimestamp(file_stat.st_ctime).strftime('%Y-%m-%d %H:%M:%S')
                modified = datetime.fromtimestamp(file_stat.st_mtime).strftime('%Y-%m-%d %H:%M:%S')

                # 记录成功读取的信息
                logger.info(f"成功读取脚本: {script_path}")

                return Response({
                    'success': True,
                    'filename': os.path.basename(script_path),  # 使用实际找到的文件名
                    'path': os.path.relpath(script_path, os.path.join(settings.BASE_DIR, 'apps', 'scripts')),
                    'content': content,
                    'step_count': len(steps),
                    'created': created,
                    'modified': modified
                })
            except Exception as e:
                logger.error(f"读取脚本 {filename} 出错: {str(e)}")
                return Response({'success': False, 'message': f'读取脚本失败: {str(e)}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        elif operation == 'write':
            # 写入脚本内容
            content = script_data.get('content')
            if not content:
                return Response({'success': False, 'message': '脚本内容不能为空'}, status=status.HTTP_400_BAD_REQUEST)

            # 创建备份
            backup_path = f"{script_path}.bak"
            shutil.copy2(script_path, backup_path)

            try:
                # 解析JSON内容，确保是有效的JSON
                script_content = json.loads(content)

                # 写入文件
                with open(script_path, 'w', encoding='utf-8') as f:
                    json.dump(script_content, f, indent=2, ensure_ascii=False)

                logger.info(f"脚本 {filename} 已更新")
                return Response({'success': True, 'message': '脚本更新成功'})

            except json.JSONDecodeError as e:
                # 恢复备份
                if os.path.exists(backup_path):
                    shutil.copy2(backup_path, script_path)

                logger.error(f"脚本 {filename} 包含无效的JSON: {str(e)}")
                return Response({'success': False, 'message': f'无效的JSON格式: {str(e)}'}, status=status.HTTP_400_BAD_REQUEST)

        else:
            return Response({'success': False, 'message': f'不支持的操作: {operation}'}, status=status.HTTP_400_BAD_REQUEST)

    except Exception as e:
        # 恢复备份
        if 'backup_path' in locals() and os.path.exists(backup_path):
            shutil.copy2(backup_path, script_path)

        logger.error(f"编辑脚本出错: {str(e)}")
        logger.error(traceback.format_exc())
        return Response({'success': False, 'message': f'处理请求失败: {str(e)}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    finally:
        # 清理备份
        if 'backup_path' in locals() and os.path.exists(backup_path):
            os.remove(backup_path)


@api_view(['POST'])
@csrf_exempt
@permission_classes([permissions.AllowAny])
def import_script(request):
    """
    导入脚本

    接收上传的JSON文件，保存为脚本文件
    支持单文件和多文件上传
    """
    try:
        # 记录请求信息
        logger.info(f"收到导入脚本请求: FILES数量: {len(request.FILES)}")
        logger.info(f"FILES键列表: {list(request.FILES.keys())}")

        # 检查是否有文件上传
        if not request.FILES:
            return api_response(code=status.HTTP_400_BAD_REQUEST, msg="请选择至少一个文件")

        # 构建保存路径
        script_dir = os.path.join(settings.BASE_DIR, 'apps', 'scripts', 'testcase')
        os.makedirs(script_dir, exist_ok=True)

        # 处理结果
        results = []
        success_count = 0
        fail_count = 0

        # 处理单文件或多文件上传
        files_to_process = []
        if 'file' in request.FILES:
            # 单文件上传
            files_to_process.append(request.FILES['file'])
            logger.info(f"处理单个文件上传: {request.FILES['file'].name}")
        else:
            # 多文件上传
            logger.info("处理多文件上传")
            for key, file in request.FILES.items():
                logger.info(f"添加文件到处理列表: {key}={file.name}")
                files_to_process.append(file)

        logger.info(f"准备处理 {len(files_to_process)} 个文件")

        # 处理每个文件
        for uploaded_file in files_to_process:
            try:
                logger.info(f"处理文件: {uploaded_file.name}")

                # 检查文件类型
                if not uploaded_file.name.endswith('.json'):
                    logger.warning(f"文件 {uploaded_file.name} 不是JSON文件")
                    results.append({
                        'filename': uploaded_file.name,
                        'success': False,
                        'message': '只支持JSON文件'
                    })
                    fail_count += 1
                    continue

                # 读取文件内容
                try:
                    content = uploaded_file.read().decode('utf-8')
                    json_data = json.loads(content)
                    logger.info(f"文件 {uploaded_file.name} 成功解析为JSON")
                except Exception as e:
                    logger.error(f"文件 {uploaded_file.name} 解析失败: {str(e)}")
                    results.append({
                        'filename': uploaded_file.name,
                        'success': False,
                        'message': f'无效的JSON文件: {str(e)}'
                    })
                    fail_count += 1
                    continue

                # 保存文件
                file_path = os.path.join(script_dir, uploaded_file.name)
                with open(file_path, 'w', encoding='utf-8') as f:
                    json.dump(json_data, f, indent=2, ensure_ascii=False)

                # 记录导入日志
                logger.info(f"脚本已导入: {uploaded_file.name}")

                # 添加成功结果
                results.append({
                    'filename': uploaded_file.name,
                    'success': True,
                    'message': '导入成功',
                    'path': os.path.join('testcase', uploaded_file.name)
                })
                success_count += 1

            except Exception as e:
                # 处理单个文件的导入错误
                logger.error(f"导入文件 {uploaded_file.name} 时出错: {str(e)}")
                results.append({
                    'filename': uploaded_file.name,
                    'success': False,
                    'message': f'导入失败: {str(e)}'
                })
                fail_count += 1

        # 返回总体结果
        return api_response(
            data=results,
            msg=f'成功导入 {success_count} 个文件，失败 {fail_count} 个文件',
        )

    except Exception as e:
        logger.error(f"导入脚本出错: {str(e)}")
        logger.error(traceback.format_exc())
        return api_response(
            code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            msg=f'导入失败: {str(e)}'
        )


@api_view(['POST'])
@csrf_exempt
@permission_classes([permissions.AllowAny])
def get_scripts(request):
    """获取脚本列表"""
    try:
        script_dir = os.path.join(settings.BASE_DIR, 'apps', 'scripts', 'testcase')
        scripts = []

        # 检查目录是否存在
        if os.path.exists(script_dir):
            for filename in os.listdir(script_dir):
                if filename.endswith('.json'):
                    file_path = os.path.join(script_dir, filename)
                    try:
                        with open(file_path, 'r', encoding='utf-8') as f:
                            script_content = json.load(f)

                        # 获取脚本信息
                        script_type = script_content.get('type', '手动')
                        script_category = script_content.get('category', '未分类')
                        steps = script_content.get('steps', [])

                        # 获取文件状态
                        file_stat = os.stat(file_path)
                        create_time = datetime.fromtimestamp(file_stat.st_ctime).strftime('%Y-%m-%d %H:%M:%S')

                        scripts.append({
                            'filename': filename,
                            'path': filename,  # 只返回文件名，不拼接任何前缀
                            'type': script_type,
                            'category': script_category,
                            'step_count': len(steps),
                            'created': create_time,  # 使用'created'与ScriptViewSet一致
                            'include_in_log': script_content.get('include_in_log', True)
                        })
                    except Exception as e:
                        logger.error(f"读取脚本 {filename} 出错: {str(e)}")

        # 按创建时间排序
        scripts.sort(key=lambda x: x['created'], reverse=True)

        # 使用辅助函数返回统一格式的响应
        return api_response(data=scripts)

    except Exception as e:
        logger.error(f"获取脚本列表出错: {str(e)}")
        return api_response(
            code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            msg=f"获取脚本列表失败: {str(e)}",
        )


class ScriptCategoryViewSet(viewsets.ModelViewSet):
    """
    脚本分类管理视图集

    提供脚本分类的CRUD操作
    """
    queryset = ScriptCategory.objects.all()
    serializer_class = ScriptCategorySerializer
    permission_classes = [AllowAny]
    http_method_names = ['post']

    def get_queryset(self):
        """
        根据查询参数过滤分类列表
        """
        queryset = ScriptCategory.objects.all()

        # 名称过滤
        name = self.request.query_params.get('name')
        if name:
            queryset = queryset.filter(name__icontains=name)

        return queryset

    def perform_create(self, serializer):
        """
        创建分类时设置作者为当前用户
        """
        serializer.save()
        logger.info(
            f"脚本分类已创建: {serializer.instance.name} - 用户: {self.request.user.username if self.request.user.is_authenticated else 'anonymous'}"
        )


class ScriptViewSet(viewsets.ModelViewSet):
    """
    脚本管理视图集

    提供脚本的CRUD操作和执行功能
    """
    queryset = Script.objects.all()
    serializer_class = ScriptSerializer
    permission_classes = [AllowAny]
    http_method_names = ['post']

    def get_serializer_class(self):
        """
        根据操作类型返回不同的序列化器
        """
        if self.action == 'create':
            return ScriptCreateSerializer
        elif self.action in ['update', 'partial_update']:
            return ScriptUpdateSerializer
        return ScriptSerializer

    def list(self, request, *args, **kwargs):
        """
        获取脚本列表
        重写为支持POST请求
        """
        try:
            script_dir = os.path.join(settings.BASE_DIR, 'apps', 'scripts', 'testcase')
            scripts = []

            # 检查目录是否存在
            if os.path.exists(script_dir):
                for filename in os.listdir(script_dir):
                    if filename.endswith('.json'):
                        file_path = os.path.join(script_dir, filename)
                        try:
                            with open(file_path, 'r', encoding='utf-8') as f:
                                script_content = json.load(f)

                            # 获取脚本信息
                            script_type = script_content.get('type', '手动')
                            script_category = script_content.get('category', '未分类')
                            steps = script_content.get('steps', [])

                            # 获取文件状态
                            file_stat = os.stat(file_path)
                            create_time = datetime.fromtimestamp(file_stat.st_ctime).strftime('%Y-%m-%d %H:%M:%S')

                            scripts.append({
                                'filename': filename,
                                'path': os.path.join('testcase', filename),
                                'type': script_type,
                                'category': script_category,
                                'step_count': len(steps),
                                'created': create_time,  # 注意: 前端期望这个字段名是'created'而不是'create_time'
                                'include_in_log': script_content.get('include_in_log', True)
                            })
                        except Exception as e:
                            logger.error(f"读取脚本 {filename} 出错: {str(e)}")

            # 按创建时间排序
            scripts.sort(key=lambda x: x['created'], reverse=True)

            # 返回使用统一格式的响应
            return api_response(data=scripts)

        except Exception as e:
            logger.error(f"获取脚本列表出错: {str(e)}")
            return api_response(
                code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                msg=f"获取脚本列表失败: {str(e)}",
            )

    def get_queryset(self):
        """
        根据查询参数过滤脚本列表
        """
        queryset = Script.objects.all()

        # 名称过滤
        name = self.request.query_params.get('name')
        if name:
            queryset = queryset.filter(name__icontains=name)

        # 类型过滤
        script_type = self.request.query_params.get('type')
        if script_type:
            queryset = queryset.filter(type=script_type)

        # 分类过滤
        category = self.request.query_params.get('category')
        if category:
            queryset = queryset.filter(category__id=category)

        # 状态过滤（是否激活）
        is_active = self.request.query_params.get('is_active')
        if is_active is not None:
            is_active = is_active.lower() == 'true'
            queryset = queryset.filter(is_active=is_active)

        # 加入日志过滤
        include_in_log = self.request.query_params.get('include_in_log')
        if include_in_log is not None:
            include_in_log = include_in_log.lower() == 'true'
            queryset = queryset.filter(include_in_log=include_in_log)

        return queryset

    def perform_create(self, serializer):
        """
        创建脚本时设置作者
        """
        if self.request.user.is_authenticated:
            serializer.save(author=self.request.user)
        else:
            serializer.save()

        logger.info(
            f"脚本已创建: {serializer.instance.name} - 用户: {self.request.user.username if self.request.user.is_authenticated else 'anonymous'}"
        )

    @action(detail=True, methods=['post'])
    def execute(self, request, pk=None):
        """
        执行脚本

        创建脚本执行记录并更新执行次数
        """
        script = self.get_object()

        # 检查脚本是否启用
        if not script.is_active:
            return Response(
                {"error": _("无法执行未启用的脚本")},
                status=status.HTTP_400_BAD_REQUEST
            )

        with transaction.atomic():
            # 创建执行记录
            execution = ScriptExecution.objects.create(
                script=script,
                status='running',
                start_time=datetime.now(),
                executed_by=request.user if request.user.is_authenticated else None
            )

            # 更新脚本执行次数
            script.execution_count += 1
            script.save(update_fields=['execution_count'])

        # 这里应该有实际的脚本执行逻辑，可能通过异步任务处理
        # 为了演示，我们假设脚本执行成功

        serializer = ScriptExecutionSerializer(execution)
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class ScriptVersionViewSet(viewsets.ReadOnlyModelViewSet):
    """脚本版本视图集 - 只读"""
    queryset = ScriptVersion.objects.all()
    serializer_class = ScriptVersionSerializer
    permission_classes = [AllowAny]
    http_method_names = ['post']

    def get_queryset(self):
        """根据脚本ID过滤版本列表"""
        queryset = ScriptVersion.objects.all()
        script_id = self.request.query_params.get('script')
        if script_id:
            queryset = queryset.filter(script_id=script_id)
        return queryset.order_by('-created_at')


class ScriptExecutionViewSet(viewsets.ReadOnlyModelViewSet):
    """
    脚本执行记录视图集

    提供脚本执行记录的查询功能
    """
    queryset = ScriptExecution.objects.all()
    serializer_class = ScriptExecutionSerializer
    permission_classes = [AllowAny]
    http_method_names = ['post']

    def get_queryset(self):
        """根据查询参数过滤执行记录"""
        queryset = ScriptExecution.objects.all()

        # 脚本过滤
        script_id = self.request.query_params.get('script')
        if script_id:
            queryset = queryset.filter(script_id=script_id)

        # 状态过滤
        status_param = self.request.query_params.get('status')
        if status_param:
            queryset = queryset.filter(status=status_param)

        # 执行人过滤
        user_id = self.request.query_params.get('user')
        if user_id:
            queryset = queryset.filter(executed_by_id=user_id)

        # 日期范围过滤
        start_date = self.request.query_params.get('start_date')
        if start_date:
            queryset = queryset.filter(start_time__gte=start_date)

        end_date = self.request.query_params.get('end_date')
        if end_date:
            queryset = queryset.filter(start_time__lte=end_date)

        return queryset.order_by('-start_time')


@api_view(['POST'])
@csrf_exempt
@permission_classes([permissions.AllowAny])
def debug_script(request):
    """
    调试脚本

    执行一个命令并返回其输出
    """
    try:
        command = request.data.get('command')
        if not command:
            return Response({'success': False, 'message': '缺少命令参数'}, status=400)

        # 记录请求信息
        logger.info(f"收到调试命令: {command}")

        # 启动子进程执行命令
        process = subprocess.Popen(
            command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding='utf-8'
        )

        # 等待一段时间，检查进程是否立即失败
        try:
            process.wait(timeout=0.5)
            stdout, stderr = process.communicate()
            success = process.returncode == 0
            message = stdout or stderr
            if not success:
                logger.error(f"命令执行失败: {stderr}")
        except subprocess.TimeoutExpired:
            # 进程还在运行，认为启动成功
            success = True
            message = "命令已启动，请在命令行窗口查看输出"
            logger.info("命令已启动，进程在后台运行")

        return Response({
            'success': success,
            'message': message,
            'pid': process.pid
        })

    except Exception as e:
        logger.error(f"执行调试命令时出错: {str(e)}")
        logger.error(traceback.format_exc())
        return Response({
            'success': False,
            'message': f"执行命令时出错: {str(e)}"
        }, status=500)
# =====================
# 任务管理系统
# =====================

import uuid
from enum import Enum
from typing import Dict, Any, Optional
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta

class TaskStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"

@dataclass
class TaskInfo:
    task_id: str
    devices: list
    scripts: list
    status: TaskStatus
    created_at: datetime
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    error_message: Optional[str] = None
    log_dir: Optional[str] = None
    results: Dict[str, Any] = None
    executor_future: Any = None

class TaskManager:
    """任务管理器 - 负责任务创建、状态跟踪、取消等功能"""

    def __init__(self):
        self.tasks: Dict[str, TaskInfo] = {}
        self.running_processes: Dict[str, Any] = {}
        self._lock = threading.RLock()

    def create_task(self, devices: list, scripts: list, log_dir: str) -> str:
        """创建新任务并返回任务ID"""
        task_id = str(uuid.uuid4())

        with self._lock:
            task_info = TaskInfo(
                task_id=task_id,
                devices=devices.copy(),
                scripts=scripts.copy(),
                status=TaskStatus.PENDING,
                created_at=datetime.now(),
                log_dir=log_dir,
                results={}
            )
            self.tasks[task_id] = task_info

        return task_id

    def update_task_status(self, task_id: str, status: TaskStatus,
                           error_message: Optional[str] = None,
                           results: Optional[Dict[str, Any]] = None):
        """更新任务状态"""
        with self._lock:
            if task_id not in self.tasks:
                return False

            task = self.tasks[task_id]
            task.status = status

            if status == TaskStatus.RUNNING and not task.started_at:
                task.started_at = datetime.now()
            elif status in [TaskStatus.COMPLETED, TaskStatus.CANCELLED, TaskStatus.FAILED]:
                task.completed_at = datetime.now()

            if error_message:
                task.error_message = error_message
            if results:
                task.results = results

        return True

    def get_task_info(self, task_id: str) -> Optional[TaskInfo]:
        """获取任务信息"""
        with self._lock:
            return self.tasks.get(task_id)

    def cancel_task(self, task_id: str) -> bool:
        """取消任务"""
        with self._lock:
            if task_id not in self.tasks:
                return False

            task = self.tasks[task_id]
            if task.status not in [TaskStatus.PENDING, TaskStatus.RUNNING]:
                return False

            # 取消正在运行的executor
            if task.executor_future and not task.executor_future.done():
                task.executor_future.cancel()

            # 终止相关子进程
            if task_id in self.running_processes:
                processes = self.running_processes[task_id]
                for proc in processes:
                    try:
                        if proc.poll() is None:  # 进程仍在运行
                            if sys.platform == "win32":
                                proc.terminate()
                                time.sleep(2)
                                if proc.poll() is None:
                                    proc.kill()
                            else:
                                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                                time.sleep(2)
                                if proc.poll() is None:
                                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    except Exception as e:
                        logger.warning(f"终止进程失败: {e}")

                del self.running_processes[task_id]

            task.status = TaskStatus.CANCELLED
            task.completed_at = datetime.now()

        return True

    def cleanup_old_tasks(self, max_age_hours: int = 24):
        """清理过期任务"""
        cutoff_time = datetime.now() - timedelta(hours=max_age_hours)

        with self._lock:
            expired_tasks = [
                task_id for task_id, task in self.tasks.items()
                if task.created_at < cutoff_time and
                   task.status in [TaskStatus.COMPLETED, TaskStatus.CANCELLED, TaskStatus.FAILED]
            ]

            for task_id in expired_tasks:
                del self.tasks[task_id]

        return len(expired_tasks)

# 全局任务管理器实例
task_manager = TaskManager()

# =====================
# 多设备并发回放主API
# =====================

@api_view(['POST'])
@csrf_exempt
@permission_classes([permissions.AllowAny])
def replay_script(request):
    """多设备并发回放指定的测试脚本"""
    # return api_response(code=0, msg="replay 成功", data={"task_id": "task-test001", "device_ids": ["1", "2", "3"]})
    try:
        import traceback
        data = json.loads(request.body)
        logger.info(f"收到多设备并发回放请求: {data}")

        # 1. 检查或获取设备参数
        devices = data.get('devices')
        if not devices:
            # 自动获取已连接设备列表
            try:
                result = run_subprocess_utf8(['adb', 'devices'], capture_output=True, check=True)
                lines = result.stdout.strip().split('\n')[1:]
                devices = [line.split()[0] for line in lines if line.strip() and 'device' in line]
            except Exception as e:
                return api_response(msg=f'获取设备列表失败: {e}', code=500)
            if not devices:
                return JsonResponse(msg='未检测到可用设备', code=400)

        # 2. 检查脚本参数，兼容scripts数组和script_path参数
        script_configs = data.get('scripts', [])
        if not script_configs and data.get('script_path'):
            # 兼容旧版API，将script_path转换为scripts数组
            script_configs = [{
                'path': data.get('script_path'),
                'loop_count': data.get('loop_count'),
                'max_duration': data.get('max_duration')
            }]

        # 检查是否指定了脚本
        if not script_configs:
            return api_response(msg='未提供脚本路径', code=400)

        # 3. 脚本路径规范化处理
        for config in script_configs:
            script_path = config.get('path')
            if not script_path:
                return api_response(msg= '脚本配置中缺少path参数', code=400)

            # 规范化脚本路径
            path_input = script_path.strip()

            # 处理相对路径
            if not os.path.isabs(path_input):
                # 简单文件名直接放入测试用例目录
                if os.sep not in path_input and '/' not in path_input:
                    path_input = os.path.join(TESTCASE_DIR, path_input)
                elif path_input.lower().startswith(('testcase', 'testcase\\', 'testcase/')):
                    # 去掉"testcase"前缀
                    if path_input.lower() == 'testcase':
                        path_input = TESTCASE_DIR
                    else:
                        subpath = path_input[len('testcase'):].lstrip('\\/') if path_input.lower().startswith('testcase') else path_input[len('testcase\\'):] if path_input.lower().startswith('testcase\\') else path_input[len('testcase/'):]
                        path_input = os.path.join(TESTCASE_DIR, subpath)
                else:
                    # 其他相对路径，视为相对于测试用例目录的路径
                    path_input = os.path.join(TESTCASE_DIR, path_input)

            # 绝对路径但是JSON文件，则确保在标准测试用例目录中
            elif path_input.lower().endswith('.json'):
                filename = os.path.basename(path_input)
                # 如果不在标准测试用例目录中，则使用文件名并放入标准目录
                if not path_input.startswith(TESTCASE_DIR):
                    path_input = os.path.join(TESTCASE_DIR, filename)

            # 规范化路径
            path_input = os.path.normpath(path_input)

            # 检查文件是否存在
            if not os.path.exists(path_input):
                return api_response(msg= f'脚本文件不存在: {path_input}', code=404)

            # 更新配置中的路径
            config['path'] = path_input

        # 4. 创建日志目录
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        log_dir_name = f"multi_device_replay_{timestamp}"
        # 迁移到 reports/tmp/replay 目录结构
        log_dir = os.path.join(DEVICE_REPORTS_DIR, 'tmp', 'replay', log_dir_name)
        try:
            os.makedirs(log_dir, exist_ok=True)
        except Exception as e_dir:
            return api_response(msg=f'创建日志目录失败: {e_dir}', code=500)
        # logger.info(f"创建日志目录: {log_dir}")

        # 5. 预先为所有设备分配账号 - 在主进程中集中处理，避免子进程竞争
        device_accounts = {}
        try:
            # 使用跨进程账号管理器
            try:
                # 检查是否为Windows系统，Windows系统不支持fcntl模块
                if platform.system() == 'Windows':
                    # Windows系统下使用普通账号管理器
                    from apps.scripts.account_manager import get_account_manager
                    account_manager = get_account_manager()
                else:
                    # Linux/Mac系统下使用跨进程账号管理器
                    from apps.scripts.cross_process_account_manager import get_cross_process_account_manager
                    account_manager = get_cross_process_account_manager()
            except ImportError as e:
                logger.error(f"导入账号管理器失败: {e}，使用备用账号管理器")
                # 导入失败时使用普通账号管理器作为备用
                from apps.scripts.account_manager import get_account_manager
                account_manager = get_account_manager()

            # 在主进程中为所有设备预分配账号
            for device_serial in devices:
                account = account_manager.allocate_account(device_serial)
                if account:
                    username, password = account
                    device_accounts[device_serial] = (username, password)
                    logger.info(f"为设备 {device_serial} 预分配账号: {username}")
                else:
                    logger.warning(f"设备 {device_serial} 账号预分配失败")
        except Exception as e:
            logger.error(f"账号预分配过程中出错: {e}")
            logger.error(traceback.format_exc())
            # 发生错误时，使用备用账号管理器
            try:
                from apps.scripts.account_manager import get_account_manager
                account_manager = get_account_manager()

                for device_serial in devices:
                    account = account_manager.allocate_account(device_serial)
                    if account:
                        username, password = account
                        device_accounts[device_serial] = (username, password)
                        logger.info(f"备用管理器: 为设备 {device_serial} 预分配账号: {username}")
            except Exception as backup_error:
                logger.error(f"备用账号管理器也失败: {backup_error}")

        # 将账号信息写入临时文件，供子进程读取
        accounts_file = os.path.join(log_dir, "device_accounts.json")
        try:
            with open(accounts_file, 'w', encoding='utf-8') as f:
                json.dump(device_accounts, f, ensure_ascii=False, indent=2)
            logger.info(f"设备账号分配信息已写入: {accounts_file}")
        except Exception as e:
            logger.error(f"写入账号分配信息失败: {e}")

        # 6. 启动设备任务
        task_id = task_manager.create_task(devices, script_configs, log_dir)
        logger.info(f"创建任务: {task_id}")        # 7. 账号预分配（集成现有账号管理器）
        from .account_manager import get_account_manager
        account_manager = get_account_manager()

        device_accounts = {}
        account_allocation_errors = []

        for device_serial in devices:
            try:
                # 调用现有的账号管理器接口
                allocated_account = account_manager.allocate_account(device_serial)
                if allocated_account:
                    device_accounts[device_serial] = {
                        'username': allocated_account[0],
                        'password': allocated_account[1]
                    }
                else:
                    account_allocation_errors.append(f"设备 {device_serial} 账号分配失败")
            except Exception as e:
                account_allocation_errors.append(f"设备 {device_serial} 账号分配异常: {str(e)}")

        # 如果有账号分配失败，取消任务并返回错误
        if account_allocation_errors:
            task_manager.update_task_status(task_id, TaskStatus.FAILED,
                                            error_message="账号分配失败")
            return JsonResponse(msg=f"账号分配失败: {account_allocation_errors}", code=400)

        # 7. 构造每个设备的任务参数
        device_tasks = {}
        for device_serial in devices:
            account_info = device_accounts[device_serial]
            script_args = []

            # 🔧 调试日志：记录脚本配置
            logger.info(f"🔍 设备 {device_serial} 开始构造任务参数")
            logger.info(f"🔍 账号信息: {account_info['username']}")
            logger.info(f"🔍 脚本配置数量: {len(script_configs)}")

            # 添加脚本参数
            for i, config in enumerate(script_configs):
                logger.info(f"🔍 处理脚本配置 {i+1}: {config}")
                script_args.extend(['--script', config['path']])
                logger.info(f"🔍 添加脚本路径: {config['path']}")

                # 添加循环次数
                loop_count = config.get('loop_count')
                if loop_count:
                    script_args.extend(['--loop-count', str(loop_count)])
                    logger.info(f"🔍 添加循环次数: {loop_count}")

                # 添加最大持续时间
                max_duration = config.get('max_duration')
                if max_duration:
                    script_args.extend(['--max-duration', str(max_duration)])
                    logger.info(f"🔍 添加最大持续时间: {max_duration}")

            # 添加账号信息
            script_args.extend(['--account', account_info['username']])
            script_args.extend(['--password', account_info['password']])

            # 🔧 关键修复：传递 task_id 确保子进程能正确写入 Redis 和数据库
            if task_id:
                script_args.extend(['--task-id', str(task_id)])

            logger.info(f"🔍 添加账号参数: {account_info['username']}")

            device_tasks[device_serial] = script_args
            logger.info(f"🔍 设备 {device_serial} 最终参数: {script_args}")
            logger.info(f"🔍 参数总数: {len(script_args)}")
            logger.info(f"🔍 ===== 设备 {device_serial} 参数构造完成 =====")
            logger.info("")

        # 8. 动态计算最佳并发数
        cpu_count = os.cpu_count() or 4
        try:
            memory_gb = psutil.virtual_memory().total / (1024**3)
            # 基于系统资源计算合理并发数
            cpu_based_limit = cpu_count * 2  # CPU核心数的2倍
            memory_based_limit = int(memory_gb / 0.5)  # 每个任务预估占用500MB内存
            system_based_limit = min(cpu_based_limit, memory_based_limit, 32)  # 系统限制最大32
        except ImportError:
            # 如果无法获取psutil，使用保守估计
            system_based_limit = min(cpu_count * 2, 16)

        max_concurrent = min(system_based_limit, len(devices), data.get('max_concurrent', system_based_limit))
        logger.info(f"计算得出最大并发数: {max_concurrent} (设备数: {len(devices)})")

        # 9. 并发执行回放任务
        results = {}
        completed_count = 0

        logger.info(f"🚀 开始并发执行回放任务，最大并发数: {max_concurrent}")
        log_step_progress(1, 4, f"开始并发执行 {len(devices)} 个设备的回放任务", None, True)
        try:
            with ThreadPoolExecutor(max_workers=max_concurrent) as executor:
                # 🔧 调试：在提交任务前记录每个设备的参数
                logger.info(f"🔧 即将提交 {len(device_tasks)} 个设备任务:")
                for device, args in device_tasks.items():
                    logger.info(f"   设备: {device}")
                    logger.info(f"   参数: {args}")
                    logger.info(f"   参数数量: {len(args)}")

                # 提交所有任务
                futures = {
                    executor.submit(run_single_replay, device, args, log_dir): device
                    for device, args in device_tasks.items()
                }

                logger.info(f"🔧 已提交 {len(futures)} 个任务到线程池")
                log_step_progress(2, 4, f"所有设备任务已提交，等待执行完成", None, True)

                # 等待任务完成并收集结果
                pending_count = len(futures)
                successful_devices = []
                failed_devices = []
                device_results = {}
                device_dirs = []  # 收集设备报告目录用于汇总报告

                # 收集设备结果
                for future in concurrent.futures.as_completed(futures):
                    device = futures[future]
                    try:
                        result = future.result()
                        results[device] = result
                        completed_count += 1

                        # 从结果中获取设备报告目录
                        device_dir = result.get('device_report_dir')
                        if device_dir:
                            # 收集有效的设备报告目录
                            if os.path.exists(device_dir):
                                device_dirs.append(device_dir)
                                logger.info(f"收集到设备 {device} 的报告目录: {device_dir}")
                            else:
                                logger.warning(f"设备 {device} 的报告目录不存在: {device_dir}")

                        # 如果没有获取到设备目录，尝试从report_url中获取
                        elif result.get('report_url'):
                            report_url = result.get('report_url')
                            # 如果report_url包含路径信息
                            if isinstance(report_url, str) and os.path.exists(report_url):
                                device_dirs.append(report_url)
                                logger.info(f"从report_url收集到设备 {device} 的报告目录: {report_url}")

                        # 记录退出码
                        exit_code = result.get('exit_code')
                        success = (exit_code == 0)
                        status_text = "成功" if success else "失败"
                        logger.info(f"✅ 设备 {device} 回放{status_text} ({completed_count}/{len(devices)})")

                        # 添加到结果数据结构
                        device_results[device] = {
                            'exit_code': exit_code,
                            'status': "success" if success else "failed",
                            'report_url': result.get('report_url')
                        }

                        # 记录设备统计信息
                        log_step_progress(len(devices) - pending_count + 1, len(devices) + 2,
                                          f"设备 {device} 执行{status_text}", device, True)

                        # 更新成功/失败设备计数
                        if success:
                            successful_devices.append(device)
                        else:
                            failed_devices.append(device)
                    except Exception as e:
                        logger.error(f"❌ 设备 {device} 执行异常: {e}")
                        device_results[device] = {
                            'exit_code': -1,
                            'status': 'error',
                            'error': str(e)
                        }
                        failed_devices.append(device)
                        completed_count += 1

                    # 减少待处理计数
                    pending_count -= 1

            # 记录日志和统计
            logger.info(f"[多设备] 步骤 3/4: 所有设备任务执行完成")

            # 释放所有设备的账号分配
            for device in devices:
                try:
                    device_name = device if isinstance(device, str) else device.get('serial')
                    if device_name and device_name in device_accounts:
                        account_info = device_accounts.get(device_name)
                        username = account_info.get('username')
                        password = account_info.get('password')
                        print(f"views 资源清理：释放账号 {device_name} 的账号分配: [{username}], [{password}]")
                        account_manager.release_account(device_name)
                        logger.info(f"资源清理：释放账号 已释放设备 {device_name} 的账号")
                except Exception as e:
                    logger.warning(f"views 释放设备账号时出错: {e}")

            # 生成汇总报告 - 在所有设备完成后由主进程统一生成
            # if device_dirs and len(device_dirs) > 0:
            #     try:
            #         # 导入报告生成器
            #         from apps.reports.report_generator import ReportGenerator
            #         from apps.reports.report_manager import ReportManager

            #         # 初始化报告管理器
            #         report_manager = ReportManager()
            #         report_generator = ReportGenerator(report_manager)

            #         logger.info(f"📊 所有设备测试完成，开始生成统一汇总报告...")

            #         # 将字符串路径转换为Path对象
            #         from pathlib import Path
            #         device_report_paths = []

            #         # 验证设备目录是否存在
            #         for dir_path in device_dirs:
            #             path = Path(dir_path)
            #             if path.exists():
            #                 device_report_paths.append(path)
            #                 logger.info(f"✅ 设备目录存在: {path}")
            #             else:
            #                 logger.warning(f"⚠️ 设备目录不存在: {path}")

            #         # 如果没有有效的设备目录，尝试查找最新的设备目录
            #         if not device_report_paths:
            #             logger.warning("⚠️ 没有有效的设备目录，尝试查找最新的设备目录...")
            #             base_dir = report_manager.single_device_reports_dir
            #             if base_dir.exists():
            #                 # 查找所有设备目录
            #                 all_device_dirs = [d for d in base_dir.iterdir() if d.is_dir()]
            #                 if all_device_dirs:
            #                     # 按修改时间排序，选择最近的目录
            #                     device_report_paths = sorted(all_device_dirs, key=lambda x: x.stat().st_mtime, reverse=True)[:len(devices)]
            #                     logger.info(f"✅ 找到 {len(device_report_paths)} 个最新设备目录")

            #         # 准备脚本配置
            #         script_info_list = []
            #         for config in script_configs:
            #             script_info_list.append({
            #                 "path": config.get("path", ""),
            #                 "loop_count": config.get("loop_count", 1),
            #                 "max_duration": config.get("max_duration")
            #             })

            #         # 1. 首先为每个设备目录生成设备报告
            #         for device_dir in device_report_paths:
            #             logger.info(f"🔄 为设备 {device_dir.name} 生成设备报告...")
            #             report_generator.generate_device_report(device_dir, script_info_list)

            #         # 2. 然后生成汇总报告
            #         summary_report_path = report_generator.generate_summary_report(device_report_paths, script_info_list)

            #         if summary_report_path:
            #             logger.info(f"✅ 统一汇总报告生成成功: {summary_report_path}")
            #         else:
            #             logger.error("❌ 统一汇总报告生成失败")
            #     except Exception as e:
            #         logger.error(f"❌ 生成统一汇总报告失败: {e}")

            #         traceback.print_exc()

            # 生成设备执行摘要
            logger.info("============================================================")

        finally:
            # 11. 资源清理：释放账号
            for device_serial in devices:
                if device_serial in device_accounts:
                    try:
                        account_info = device_accounts.get(device_serial)
                        username = account_info.get('username')
                        password = account_info.get('password')
                        print(f"views 资源清理：释放账号 {device_serial} 的账号分配: [{username}], [{password}]")
                        account_manager.release_account(device_serial)
                        logger.info(f"资源清理：释放账号 已释放设备 {device_serial} 的账号")
                    except Exception as e:
                        logger.warning(f"资源清理：释放账号 设备 {device_serial} 账号释放失败: {e}")

        # 11. 确保所有设备都有结果记录
        for device in devices:
            if device not in results:
                results[device] = {
                    "error": "任务未执行或丢失",
                    "exit_code": -1,
                    "report_url": "",
                    "device": device,
                    "log_url": f"/static/reports/{log_dir_name}/{device}.log"
                }

        # 12. 构建响应数据，使用先前创建的 task_id
        response_data = {
            "success": True,
            "task_id": task_id,
            "message": f"多设备并发回放完成，共处理 {len(devices)} 台设备",
            "log_dir": log_dir,
            "results": []
        }
        # 转换结果格式
        for device, result in results.items():
            device_result = {
                "device": device,
                "status": "completed" if result.get("exit_code") == 0 else "failed",
                "exit_code": result.get("exit_code", -1),
                "report_url": result.get("report_url", ""),
                "log_url": f"/static/reports/{log_dir_name}/{device}.log",
                "error": result.get("error", "")
            }
            response_data["results"].append(device_result)

        # 生成设备执行汇总日志
        log_device_summary(list(results.values()))
        log_step_progress(4, 4, f"任务完成，成功: {sum(1 for r in results.values() if r.get('exit_code') == 0)}/{len(devices)}", None, True)

        logger.info(f"多设备并发回放任务完成: {len(devices)} 台设备，成功: {sum(1 for r in results.values() if r.get('exit_code') == 0)}")
        return api_response(data=response_data)

    except Exception as e:
        error_msg = str(e)
        logger.error(f"多设备并发回放失败: {error_msg}")
        logger.error(f"详细错误信息: {traceback.format_exc()}")

        return api_response(code=500,msg=f"多设备并发回放失败: {error_msg}")

# =====================
# 步骤级日志记录函数
# =====================

def log_step_progress(step_num, total_steps, message, device_name=None, is_multi_device=False):
    """
    统一的步骤进度日志记录函数
    适用于单设备和多设备场景
    """
    if is_multi_device and device_name:
        prefix = f"[设备:{device_name}]"
    else:
        prefix = "[单设备]" if not is_multi_device else "[多设备]"

    progress_indicator = f"步骤 {step_num}/{total_steps}"
    logger.info(f"{prefix} {progress_indicator}: {message}")

def log_phase_start(phase_name, device_name=None, is_multi_device=False):
    """记录阶段开始"""
    if is_multi_device and device_name:
        prefix = f"[设备:{device_name}]"
    else:
        prefix = "[单设备]" if not is_multi_device else "[多设备]"

    logger.info(f"{prefix} 🚀 开始阶段: {phase_name}")

def log_phase_complete(phase_name, device_name=None, is_multi_device=False, success=True):
    """记录阶段完成"""
    if is_multi_device and device_name:
        prefix = f"[设备:{device_name}]"
    else:
        prefix = "[单设备]" if not is_multi_device else "[多设备]"

    status = "✅ 完成" if success else "❌ 失败"
    logger.info(f"{prefix} {status}阶段: {phase_name}")

def log_device_summary(device_results):
    """记录多设备执行汇总"""
    if not device_results:
        logger.info("📊 [汇总] 无设备执行结果")
        return

    total_devices = len(device_results)
    successful_devices = sum(1 for result in device_results if result.get('exit_code', -1) == 0)
    failed_devices = total_devices - successful_devices

    logger.info("=" * 60)
    logger.info("📊 [执行汇总]")
    logger.info(f"   总设备数: {total_devices}")
    logger.info(f"   成功设备: {successful_devices}")
    logger.info(f"   失败设备: {failed_devices}")
    logger.info(f"   成功率: {(successful_devices/total_devices*100):.1f}%")

    for i, result in enumerate(device_results):
        device_name = result.get('device', f'设备{i+1}')
        status = "✅" if result.get('exit_code', -1) == 0 else "❌"
        logger.info(f"   {status} {device_name}")
    logger.info("=" * 60)


@api_view(['POST'])
@csrf_exempt
@permission_classes([permissions.AllowAny])
def get_devices(request):
    """
    获取已连接设备列表
    """
    try:
        # 获取设备信息的简单实现
        devices = []

        # 尝试使用adb获取设备列表
        try:
            cmd = "adb devices"
            process = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            stdout, stderr = process.communicate()

            if process.returncode == 0:
                # 解析adb devices输出
                lines = stdout.strip().split('\n')
                if len(lines) > 1:  # 第一行是标题
                    for line in lines[1:]:
                        if line.strip():
                            parts = line.split('\t')
                            if len(parts) >= 2:
                                device_id = parts[0].strip()
                                status = parts[1].strip()

                                # 获取设备型号
                                model = ""
                                try:
                                    model_cmd = f'adb -s {device_id} shell getprop ro.product.model'
                                    model_proc = subprocess.Popen(model_cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                                    model, _ = model_proc.communicate()
                                    model = model.strip()
                                except Exception:
                                    pass

                                devices.append({
                                    'id': device_id,
                                    'status': status,
                                    'model': model,
                                    'platform': 'Android'
                                })
        except Exception as e:
            logger.error(f"获取设备列表出错: {str(e)}")

        return Response(devices)

    except Exception as e:
        logger.error(f"获取设备列表时发生错误: {str(e)}")
        return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['POST'])
@csrf_exempt
@permission_classes([permissions.AllowAny])
def get_reports(request):
    """
    获取测试报告列表
    """
    try:
        # 报告目录
        reports_dir = os.path.join(settings.BASE_DIR, 'staticfiles', 'reports', 'summary_reports')
        if not os.path.exists(reports_dir):
            os.makedirs(reports_dir, exist_ok=True)

        # 收集报告信息
        reports = []

        if os.path.exists(reports_dir):
            # 列出所有HTML文件
            for filename in os.listdir(reports_dir):
                if filename.endswith('.html'):
                    file_path = os.path.join(reports_dir, filename)
                    file_stat = os.stat(file_path)

                    # 从文件名提取信息
                    try:
                        # 假设文件名格式为: report_YYYY-MM-DD_HH-MM-SS.html
                        parts = filename.replace('.html', '').split('_')
                        if len(parts) >= 2:
                            date_time_str = '_'.join(parts[1:])
                            # 尝试解析日期时间
                            report_date = datetime.strptime(date_time_str, '%Y-%m-%d_%H-%M-%S')
                        else:
                            # 如果文件名格式不符合预期，使用文件修改时间
                            report_date = datetime.fromtimestamp(file_stat.st_mtime)
                    except Exception:
                        # 解析失败时使用文件修改时间
                        report_date = datetime.fromtimestamp(file_stat.st_mtime)

                    reports.append({
                        'filename': filename,
                        'path': f'/reports/summary_reports/{filename}',
                        'date': report_date.strftime('%Y-%m-%d %H:%M:%S'),
                        'size': file_stat.st_size
                    })

        # 按日期排序，最新的在前
        reports.sort(key=lambda x: x['date'], reverse=True)

        return Response(reports)

    except Exception as e:
        logger.error(f"获取报告列表时出错: {str(e)}")
        return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['POST'])
@csrf_exempt
@permission_classes([permissions.AllowAny])
def get_latest_report(request):
    """
    获取最新的测试报告
    """
    try:
        # 报告目录
        reports_dir = os.path.join(settings.BASE_DIR, 'staticfiles', 'reports', 'summary_reports')
        if not os.path.exists(reports_dir):
            return Response({'error': '未找到报告目录'}, status=status.HTTP_404_NOT_FOUND)

        # 查找最新报告
        latest_report = None
        latest_time = None

        for filename in os.listdir(reports_dir):
            if filename.endswith('.html'):
                file_path = os.path.join(reports_dir, filename)
                file_time = os.path.getmtime(file_path)

                if latest_time is None or file_time > latest_time:
                    latest_time = file_time
                    latest_report = {
                        'filename': filename,
                        'path': f'/reports/summary_reports/{filename}',
                        'date': datetime.fromtimestamp(file_time).strftime('%Y-%m-%d %H:%M:%S'),
                        'size': os.path.getsize(file_path)
                    }

        if latest_report:
            return Response(latest_report)
        else:
            return Response({'error': '未找到任何报告'}, status=status.HTTP_404_NOT_FOUND)

    except Exception as e:
        logger.error(f"获取最新报告时出错: {str(e)}")
        return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['POST'])
@csrf_exempt
@permission_classes([permissions.AllowAny])
def get_replay_snapshot(request):
    """返回指定任务的回放历史快照（优先从 Redis，其次回退数据库 ReportDetail）。

    请求体:
    {
      "task_id": "<taskId>",
      "device": "<optional device serial>"
    }

    响应:
    {
      "task_id": "...",
      "devices": [{"device": "serial", "records": [...]}],
      "ts": <epoch_ms>
    }
    """
    try:
        data = request.data or {}
        task_id = str(data.get('task_id') or '').strip()
        device = str(data.get('device') or '').strip()
        if not task_id:
            return api_response(code=400, msg='缺少 task_id')

        entries = []

        # 1) 优先从 Redis 读取（运行中优先显示最新快照）
        try:
            redis_client = getattr(settings, 'REDIS', None)
            redis_client = getattr(redis_client, 'client', None)
        except Exception:
            redis_client = None

        if redis_client:
            if device:
                key = f"wfgame:replay:task:{task_id}:device:{device}:steps"
                raw = redis_client.get(key)
                records = []
                if raw:
                    try:
                        val = raw.decode('utf-8') if isinstance(raw, (bytes, bytearray)) else raw
                        records = json.loads(val)
                    except Exception:
                        records = []

                # 尝试获取 Redis 中的错误信息
                err_msg = ""
                try:
                    err_key = f"wfgame:replay:task:{task_id}:device:{device}:error"
                    err_raw = redis_client.get(err_key)
                    if err_raw:
                        err_msg = err_raw.decode('utf-8') if isinstance(err_raw, (bytes, bytearray)) else str(err_raw)
                except Exception:
                    pass

                entries.append({"device": device, "records": records, "error_message": err_msg})
            else:
                pattern = f"wfgame:replay:task:{task_id}:device:*:steps"
                try:
                    for key in redis_client.scan_iter(match=pattern):
                        k = key.decode('utf-8') if isinstance(key, (bytes, bytearray)) else str(key)
                        parts = k.split(':')
                        serial = parts[-2] if len(parts) >= 2 else ''
                        raw = redis_client.get(key)
                        records = []
                        if raw:
                            try:
                                val = raw.decode('utf-8') if isinstance(raw, (bytes, bytearray)) else raw
                                records = json.loads(val)
                            except Exception:
                                records = []

                        # 尝试获取 Redis 中的错误信息
                        err_msg = ""
                        try:
                            err_key = f"wfgame:replay:task:{task_id}:device:{serial}:error"
                            err_raw = redis_client.get(err_key)
                            if err_raw:
                                err_msg = err_raw.decode('utf-8') if isinstance(err_raw, (bytes, bytearray)) else str(err_raw)
                        except Exception as e:
                            logger.warning(f"获取Redis错误信息失败: {e}")

                        entries.append({"device": serial, "records": records, "error_message": err_msg})
                except Exception:
                    # 忽略扫描 Redis 失败，继续回退逻辑
                    pass

        # 2) 从数据库 ReportDetail（历史快照）补充或回退
        #  - 若 Redis 没有任何数据，则直接使用 DB 快照
        #  - 若 Redis 有数据，则额外拼接 DB 的 error_message 字段，便于前端展示系统级错误
        if not entries:
            try:
                from apps.reports.models import ReportDetail
                qs = (ReportDetail.objects.all_teams()
                      .select_related('report', 'device')
                      .filter(report__task_id=int(task_id)))
                if device:
                    qs = qs.filter(device__device_id=device)
                for d in qs:
                    try:
                        serial = getattr(d.device, 'device_id', '')
                    except Exception:
                        serial = ''
                    records = getattr(d, 'step_results', None) or []
                    # 防御式：确保为列表，避免旧数据为 {} 导致前端解析问题
                    if not isinstance(records, list):
                        records = []
                    entries.append({'device': serial, 'records': records, 'error_message': getattr(d, 'error_message', '') or ''})
            except Exception as _db_err:
                logger.warning(f"读取数据库快照失败: {_db_err}")
        else:
            # Redis 有 entries 时，追加 DB 的 error_message 信息
            try:
                from apps.reports.models import ReportDetail
                serials = [e.get('device') for e in entries if e.get('device')]
                if serials:
                    qs = (ReportDetail.objects.all_teams()
                          .select_related('report', 'device')
                          .filter(report__task_id=int(task_id), device__device_id__in=serials)
                          .only('error_message', 'device__device_id'))
                    emap = {}
                    for d in qs:
                        try:
                            emap[getattr(d.device, 'device_id', '')] = getattr(d, 'error_message', '') or ''
                        except Exception:
                            continue
                    for e in entries:
                        dev = e.get('device')
                        # 如果 Redis 中没有错误信息（或为空），尝试从 DB 补充
                        if dev and not e.get('error_message'):
                            db_err = emap.get(dev, '')
                            if db_err:
                                e['error_message'] = db_err
            except Exception as _db_err2:
                logger.debug("补充 error_message 失败: %s", _db_err2)

        return api_response(data={
            'task_id': task_id,
            'devices': entries,
            'ts': int(time.time() * 1000)
        })
    except Exception as e:
        logger.error(f"获取回放快照失败: {e}")
        return api_response(code=500, msg=f"获取回放快照失败: {e}")


@api_view(['POST'])
@csrf_exempt
@permission_classes([permissions.AllowAny])
def record_script(request):
    """
    录制脚本

    接收设备ID和录制参数，启动录制过程
    """
    try:
        # 获取请求参数
        device_id = request.data.get('device_id')
        name = request.data.get('name', 'recorded_script')
        record_clicks = request.data.get('record_clicks', True)
        record_swipes = request.data.get('record_swipes', True)

        if not device_id:
            return Response({'success': False, 'message': '请提供设备ID'}, status=400)

        # 构建命令
        command = f"python record_script.py --device {device_id} --name {name}"

        # 添加录制选项
        if record_clicks:
            command += " --record-clicks"
        if record_swipes:
            command += " --record-swipes"

        # 记录请求信息
        logger.info(f"收到录制命令: {command}")

        # 启动子进程执行命令
        process = subprocess.Popen(
            command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding='utf-8'
        )

        # 等待一段时间，检查进程是否立即失败
        try:
            process.wait(timeout=0.5)
            stdout, stderr = process.communicate()
            success = process.returncode == 0
            message = stdout or stderr
            if not success:
                logger.error(f"录制脚本命令执行失败: {stderr}")
        except subprocess.TimeoutExpired:
            # 进程还在运行，认为启动成功
            success = True
            message = "录制已启动，请在命令行窗口查看输出"
            logger.info("录制已启动，进程在后台运行")

        return Response({
            'success': success,
            'message': message,
            'pid': process.pid
        })

    except Exception as e:
        logger.error(f"执行录制命令时出错: {str(e)}")
        logger.error(traceback.format_exc())
        return Response({
            'success': False,
            'message': f"执行录制命令时出错: {str(e)}"
        }, status=500)


@api_view(['POST'])
@csrf_exempt
@permission_classes([permissions.AllowAny])
def replay_status(request):
    """
    获取回放状态

    返回正在运行的回放任务的状态
    """
    try:
        task_id = request.data.get('task_id')
        if not task_id:
            # 如果没有提供任务ID，返回所有任务的状态
            if task_manager:
                tasks = []
                for tid, task_info in task_manager._tasks.items():
                    tasks.append({
                        'id': tid,
                        'status': task_info.status.value,
                        'devices': task_info.devices,
                        'scripts': task_info.scripts,
                        'created_at': task_info.created_at.strftime('%Y-%m-%d %H:%M:%S'),
                        'started_at': task_info.started_at.strftime('%Y-%m-%d %H:%M:%S') if task_info.started_at else None,
                        'completed_at': task_info.completed_at.strftime('%Y-%m-%d %H:%M:%S') if task_info.completed_at else None,
                        'error_message': task_info.error_message
                    })
                return Response({'tasks': tasks})
            else:
                return Response({'error': '任务管理器未初始化'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        else:
            # 返回特定任务的状态
            if task_manager:
                task_info = task_manager.get_task_info(task_id)
                if task_info:
                    return Response({
                        'id': task_id,
                        'status': task_info.status.value,
                        'devices': task_info.devices,
                        'scripts': task_info.scripts,
                        'created_at': task_info.created_at.strftime('%Y-%m-%d %H:%M:%S'),
                        'started_at': task_info.started_at.strftime('%Y-%m-%d %H:%M:%S') if task_info.started_at else None,
                        'completed_at': task_info.completed_at.strftime('%Y-%m-%d %H:%M:%S') if task_info.completed_at else None,
                        'error_message': task_info.error_message,
                        'results': task_info.results
                    })
                else:
                    return Response({'error': f'未找到任务: {task_id}'}, status=status.HTTP_404_NOT_FOUND)
            else:
                return Response({'error': '任务管理器未初始化'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    except Exception as e:
        logger.error(f"获取回放状态时出错: {str(e)}")
        return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['POST'])
@csrf_exempt
@permission_classes([permissions.AllowAny])
def replay_cancel(request):
    """
    取消回放任务

    停止正在运行的回放任务
    """
    try:
        task_id = request.data.get('task_id')
        if not task_id:
            return Response({'error': '请提供任务ID'}, status=status.HTTP_400_BAD_REQUEST)

        if not task_manager:
            return Response({'error': '任务管理器未初始化'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        success = task_manager.cancel_task(task_id)

        if success:
            return Response({'success': True, 'message': f'任务 {task_id} 已取消'})
        else:
            return Response({'success': False, 'message': f'取消任务 {task_id} 失败，可能任务不存在或已完成'})

    except Exception as e:
        logger.error(f"取消回放任务时出错: {str(e)}")
        return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['POST'])
@csrf_exempt
@permission_classes([permissions.AllowAny])
def start_record(request):
    """
    开始录制

    启动新的录制会话
    """
    try:
        # 获取请求参数
        device_id = request.data.get('device')
        record_name = request.data.get('name', 'recorded_script')

        if not device_id:
            return Response({'success': False, 'message': '请提供设备ID'}, status=400)

        # 构建录制命令
        command = f"python record_script.py --device {device_id} --name {record_name} --record"

        logger.info(f"开始录制命令: {command}")

        # 启动录制进程
        process = subprocess.Popen(
            command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding='utf-8'
        )

        # 等待一段时间，检查进程是否立即失败
        try:
            process.wait(timeout=1)
            stdout, stderr = process.communicate()
            success = process.returncode == 0
            message = stdout or stderr
            if not success:
                logger.error(f"录制启动失败: {stderr}")
                return Response({'success': False, 'message': f'录制启动失败: {stderr}'})
        except subprocess.TimeoutExpired:
            # 进程还在运行，认为启动成功
            success = True

        # 创建线程读取输出
        def read_output(stream, log_func):
            for line in iter(stream.readline, ''):
                if line:
                    log_func(line.strip())
            stream.close()

        if success:
            # 创建线程读取stdout
            stdout_thread = Thread(target=read_output, args=(process.stdout, logger.info))
            stdout_thread.daemon = True
            stdout_thread.start()

            # 创建线程读取stderr
            stderr_thread = Thread(target=read_output, args=(process.stderr, logger.error))
            stderr_thread.daemon = True
            stderr_thread.start()

            return Response({
                'success': True,
                'message': '录制已开始，请按下Ctrl+C停止录制',
                'pid': process.pid
            })
        else:
            return Response({
                'success': False,
                'message': '录制启动失败',
                'error': message
            })

    except Exception as e:
        logger.error(f"启动录制时出错: {str(e)}")
        logger.error(traceback.format_exc())
        return Response({
            'success': False,
            'message': f"启动录制时出错: {str(e)}"
        }, status=500)


@api_view(['POST'])
@csrf_exempt
@permission_classes([permissions.AllowAny])
def get_python_envs(request):
    """
    获取可用的Python环境列表
    """
    try:
        python_envs = []

        # 查找系统中的Python解释器
        if platform.system() == "Windows":
            # Windows系统查找
            paths = [
                r"C:\Python39\python.exe",
                r"C:\Python310\python.exe",
                r"C:\Python311\python.exe",
                r"C:\Python312\python.exe",
                r"C:\Users\Administrator\AppData\Local\Programs\Python\Python39\python.exe",
                r"C:\Users\Administrator\AppData\Local\Programs\Python\Python310\python.exe",
                r"C:\Users\Administrator\AppData\Local\Programs\Python\Python311\python.exe",
                r"C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe"
            ]

            # Anaconda环境
            anaconda_paths = [
                r"C:\ProgramData\Anaconda3\python.exe",
                r"C:\Users\Administrator\anaconda3\python.exe"
            ]

            # 添加当前激活的Python环境
            current_path = sys.executable
            if current_path not in paths and current_path not in anaconda_paths:
                paths.append(current_path)

            # 检查路径是否存在
            for path in paths + anaconda_paths:
                if os.path.exists(path):
                    try:
                        # 获取Python版本
                        result = subprocess.run([path, "--version"],
                                                stdout=subprocess.PIPE,
                                                stderr=subprocess.PIPE,
                                                text=True,
                                                check=False)
                        version = result.stdout or result.stderr
                        version = version.strip()

                        python_envs.append({
                            'path': path,
                            'version': version,
                            'is_current': path == current_path
                        })
                    except Exception as e:
                        logger.error(f"获取Python版本出错 {path}: {str(e)}")
        else:
            # Linux/Mac系统查找
            paths = [
                "/usr/bin/python3",
                "/usr/local/bin/python3",
                "/opt/homebrew/bin/python3"
            ]

            # 添加当前激活的Python环境
            current_path = sys.executable
            if current_path not in paths:
                paths.append(current_path)

            # 检查路径是否存在
            for path in paths:
                if os.path.exists(path):
                    try:
                        # 获取Python版本
                        result = subprocess.run([path, "--version"],
                                                stdout=subprocess.PIPE,
                                                stderr=subprocess.PIPE,
                                                text=True,
                                                check=False)
                        version = result.stdout or result.stderr
                        version = version.strip()

                        python_envs.append({
                            'path': path,
                            'version': version,
                            'is_current': path == current_path
                        })
                    except Exception as e:
                        logger.error(f"获取Python版本出错 {path}: {str(e)}")

        return Response(python_envs)

    except Exception as e:
        logger.error(f"获取Python环境列表出错: {str(e)}")
        return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['POST'])
@csrf_exempt
@permission_classes([permissions.AllowAny])
def switch_python_env(request):
    """
    切换Python环境

    接收新的Python解释器路径，更新配置文件
    """
    try:
        # 获取新的Python路径
        python_path = request.data.get('path')
        if not python_path:
            return Response({'success': False, 'message': '请提供Python解释器路径'}, status=400)

        # 检查路径是否存在且可执行
        if not os.path.exists(python_path):
            return Response({'success': False, 'message': '指定的Python解释器不存在'}, status=400)

        # 验证是否为有效的Python解释器
        try:
            result = subprocess.run([python_path, "--version"],
                                    stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE,
                                    text=True,
                                    check=False)

            if result.returncode != 0:
                return Response({'success': False, 'message': '无效的Python解释器'}, status=400)

            version = result.stdout or result.stderr
            version = version.strip()
        except Exception as e:
            logger.error(f"验证Python解释器出错: {str(e)}")
            return Response({'success': False, 'message': f'验证Python解释器出错: {str(e)}'}, status=400)

        # 更新配置文件
        config_path = os.path.join(settings.BASE_DIR, 'config.ini')
        config = {}

        # 读取现有配置
        if os.path.exists(config_path):
            try:
                with open(config_path, 'r', encoding='utf-8') as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith('#') and '=' in line:
                            key, value = line.split('=', 1)
                            config[key.strip()] = value.strip()
            except Exception as e:
                logger.error(f"读取配置文件出错: {str(e)}")

        # 更新Python路径
        config['PYTHON_PATH'] = python_path

        # 保存配置
        try:
            with open(config_path, 'w', encoding='utf-8') as f:
                for key, value in config.items():
                    f.write(f"{key}={value}\n")
        except Exception as e:
            logger.error(f"保存配置文件出错: {str(e)}")
            return Response({'success': False, 'message': f'保存配置文件出错: {str(e)}'}, status=500)

        return Response({
            'success': True,
            'message': f'已切换到Python环境: {version}',
            'path': python_path,
            'version': version
        })

    except Exception as e:
        logger.error(f"切换Python环境出错: {str(e)}")
        return Response({'success': False, 'message': f'切换Python环境出错: {str(e)}'}, status=500)


@api_view(['POST'])
@csrf_exempt
@permission_classes([permissions.AllowAny])
def storage_status(request):
    """
    获取存储状态信息

    返回日志目录、缓存目录等的占用情况
    """
    try:
        # 存储信息
        storage_info = {}

        # 获取磁盘使用情况
        if platform.system() == 'Windows':
            drive = os.path.splitdrive(settings.BASE_DIR)[0] or 'C:'
            total, used, free = shutil.disk_usage(drive)
            storage_info['disk'] = {
                'total': total,
                'used': used,
                'free': free,
                'used_percent': round(used / total * 100, 2),
                'drive': drive
            }
        else:
            # Linux/Mac
            total, used, free = shutil.disk_usage('/')
            storage_info['disk'] = {
                'total': total,
                'used': used,
                'free': free,
                'used_percent': round(used / total * 100, 2),
                'drive': '/'
            }

        # 获取项目主要目录大小
        dirs_to_check = {
            'logs': os.path.join(settings.BASE_DIR, 'logs'),
            'staticfiles': os.path.join(settings.BASE_DIR, 'staticfiles'),
            'media': os.path.join(settings.BASE_DIR, 'media'),
            'reports': os.path.join(settings.BASE_DIR, 'staticfiles', 'reports'),
            'temp': tempfile.gettempdir()
        }

        dir_sizes = {}
        for name, path in dirs_to_check.items():
            if os.path.exists(path):
                size = 0
                file_count = 0

                try:
                    for dirpath, dirnames, filenames in os.walk(path):
                        for f in filenames:
                            fp = os.path.join(dirpath, f)
                            if os.path.exists(fp):
                                size += os.path.getsize(fp)
                                file_count += 1

                    dir_sizes[name] = {
                        'size': size,
                        'file_count': file_count,
                        'path': path
                    }
                except Exception as e:
                    logger.error(f"计算目录 {name} 大小时出错: {str(e)}")
                    dir_sizes[name] = {
                        'size': -1,
                        'file_count': -1,
                        'path': path,
                        'error': str(e)
                    }

        storage_info['directories'] = dir_sizes

        return Response(storage_info)

    except Exception as e:
        logger.error(f"获取存储状态时出错: {str(e)}")
        return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['POST'])
@csrf_exempt
@permission_classes([permissions.AllowAny])
def storage_cleanup(request):
    """
    清理存储空间

    清理日志、临时文件等
    """
    try:
        cleanup_type = request.data.get('type', 'all')
        max_age_days = request.data.get('max_age_days', 7)

        results = {
            'success': True,
            'cleaned': {}
        }

        # 获取当前时间
        now = datetime.now()
        cutoff_time = now - timedelta(days=max_age_days)

        # 清理日志
        if cleanup_type in ['logs', 'all']:
            logs_dir = os.path.join(settings.BASE_DIR, 'logs')

            if os.path.exists(logs_dir):
                cleaned_files = 0
                cleaned_size = 0

                for dirpath, dirnames, filenames in os.walk(logs_dir):
                    for filename in filenames:
                        file_path = os.path.join(dirpath, filename)

                        try:
                            # 跳过当前使用的日志文件
                            if filename == 'django.log':
                                continue

                            file_time = datetime.fromtimestamp(os.path.getmtime(file_path))

                            if file_time < cutoff_time:
                                file_size = os.path.getsize(file_path)
                                os.remove(file_path)
                                cleaned_files += 1
                                cleaned_size += file_size
                        except Exception as e:
                            logger.error(f"清理日志文件 {file_path} 时出错: {str(e)}")

                results['cleaned']['logs'] = {
                    'files_removed': cleaned_files,
                    'bytes_freed': cleaned_size
                }

        # 清理临时文件
        if cleanup_type in ['temp', 'all']:
            temp_dir = os.path.join(settings.BASE_DIR, 'temp')

            if os.path.exists(temp_dir):
                cleaned_files = 0
                cleaned_size = 0

                for dirpath, dirnames, filenames in os.walk(temp_dir):
                    for filename in filenames:
                        file_path = os.path.join(dirpath, filename)

                        try:
                            file_time = datetime.fromtimestamp(os.path.getmtime(file_path))

                            if file_time < cutoff_time:
                                file_size = os.path.getsize(file_path)
                                os.remove(file_path)
                                cleaned_files += 1
                                cleaned_size += file_size
                        except Exception as e:
                            logger.error(f"清理临时文件 {file_path} 时出错: {str(e)}")

                results['cleaned']['temp'] = {
                    'files_removed': cleaned_files,
                    'bytes_freed': cleaned_size
                }

        # 清理旧报告
        if cleanup_type in ['reports', 'all']:
            reports_dir = os.path.join(settings.BASE_DIR, 'staticfiles', 'reports')

            if os.path.exists(reports_dir):
                cleaned_files = 0
                cleaned_size = 0

                for dirpath, dirnames, filenames in os.walk(reports_dir):
                    for filename in filenames:
                        # 只清理HTML文件
                        if not filename.endswith('.html'):
                            continue

                        file_path = os.path.join(dirpath, filename)

                        try:
                            file_time = datetime.fromtimestamp(os.path.getmtime(file_path))

                            if file_time < cutoff_time:
                                file_size = os.path.getsize(file_path)
                                os.remove(file_path)
                                cleaned_files += 1
                                cleaned_size += file_size
                        except Exception as e:
                            logger.error(f"清理报告文件 {file_path} 时出错: {str(e)}")

                results['cleaned']['reports'] = {
                    'files_removed': cleaned_files,
                    'bytes_freed': cleaned_size
                }

        # 返回清理结果
        return Response(results)

    except Exception as e:
        logger.error(f"清理存储空间时出错: {str(e)}")
        return Response({'success': False, 'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['POST'])
@csrf_exempt
@permission_classes([permissions.AllowAny])
def delete_script(request):
    """
    删除脚本

    接收脚本文件名，删除对应的脚本文件
    """
    import os  # 确保在函数开始就导入os模块

    try:
        data = request.data
        filename = data.get('filename', '')

        # 打印完整的请求数据
        logger.error(f"删除脚本请求request参数: {request.data}")
        logger.error(f"删除脚本请求参数: {data}")
        logger.error(f"原始文件名: '{filename}'")

        # 处理转义字符问题 - 修复\t被解析为制表符的问题
        if '\t' in filename:
            filename = filename.replace('\t', '\\t')

        # 如果filename中包含路径分隔符，只取文件名部分
        if '\\' in filename:
            filename = filename.split('\\')[-1]
        elif '/' in filename:
            filename = filename.split('/')[-1]

        # 去除文件名中多余的空格
        if filename:
            filename = filename.strip()
            logger.error(f"处理后的文件名: '{filename}'")

        # 首先检查文件是否存在
        script_dirs = [
            os.path.join(settings.BASE_DIR, 'apps', 'scripts', 'testcase'),
            os.path.join(settings.BASE_DIR, 'apps', 'scripts')
        ]

        script_path = None
        for script_dir in script_dirs:
            temp_path = os.path.join(script_dir, filename)
            logger.error(f"检查文件路径: '{temp_path}'")
            if os.path.exists(temp_path):
                script_path = temp_path
                break

        if script_path is None:
            # 记录找不到文件的详细信息
            logger.error(f"找不到脚本文件: '{filename}'，已检查目录: {script_dirs}")
            return api_response(
                code=status.HTTP_404_NOT_FOUND,
                msg=f"找不到脚本文件: {filename}",
            )

        # 删除脚本文件
        os.remove(script_path)
        logger.info(f"脚本文件已删除: '{script_path}'")

        # 返回成功响应
        return api_response()
    except Exception as e:
        logger.exception(f"删除脚本时发生错误: {str(e)}")
        return api_response(
            code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            msg=f"删除失败: {str(e)}",
        )


# =====================
# UTF-8 subprocess 封装函数
# =====================
def run_subprocess_utf8(cmd, **kwargs):
    """
    统一的UTF-8 subprocess封装函数
    确保所有子进程调用都使用正确的UTF-8编码
    """
    # 强制设置UTF-8相关参数
    utf8_kwargs = {
        'encoding': 'utf-8',
        'errors': 'replace',
        'text': True,
        'env': dict(os.environ, PYTHONIOENCODING='utf-8', PYTHONUTF8='1')
    }

    # 合并用户提供的参数，但UTF-8设置优先
    final_kwargs = {**kwargs, **utf8_kwargs}

    try:
        return subprocess.run(cmd, **final_kwargs)
    except Exception as e:
        logger.error(f"subprocess执行失败: {cmd}, 错误: {e}")
        raise

def create_subprocess_utf8(cmd, **kwargs):
    """
    统一的UTF-8 subprocess.Popen封装函数
    确保所有子进程创建都使用正确的UTF-8编码
    """
    # 强制设置UTF-8相关参数
    utf8_kwargs = {
        'encoding': 'utf-8',
        'errors': 'replace',
        'text': True,
        'env': dict(os.environ, PYTHONIOENCODING='utf-8', PYTHONUTF8='1')
    }

    # 合并用户提供的参数，但UTF-8设置优先
    final_kwargs = {**kwargs, **utf8_kwargs}

    try:
        return subprocess.Popen(cmd, **final_kwargs)
    except Exception as e:
        logger.error(f"subprocess.Popen创建失败: {cmd}, 错误: {e}")
        raise

# 获取当前python解释器路径（仅返回sys.executable）
def get_persistent_python_path():
    """
    获取当前python解释器路径，已静态化
    """
    return sys.executable

# =====================
# 方案3实现 - run_single_replay 函数
# =====================

def check_device_available(device_serial):
    """检查设备是否可用且未被占用"""
    try:
        # 检查 adb 设备状态
        result = subprocess.run(['adb', 'devices'], capture_output=True, text=True, timeout=10)
        if device_serial in result.stdout and 'device' in result.stdout:
            # 进一步检查设备是否被锁定（可根据实际情况实现）
            logger.info(f"设备 {device_serial} 状态检查通过")
            return True
        else:
            logger.warning(f"设备 {device_serial} 未在adb设备列表中找到")
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        logger.error(f"设备 {device_serial} 状态检查失败: {e}")
    return False

def run_single_replay(device_serial, script_args, log_dir, timeout=3600, max_retries=2):
    """
    为单个设备启动回放脚本子进程，并监控其执行。
    包含完整的错误处理、重试机制和资源清理。

    Args:
        device_serial: 设备序列号
        script_args: 脚本参数列表
        log_dir: 日志目录
        timeout: 超时时间（秒）
        max_retries: 最大重试次数

    Returns:
        dict: 执行结果 {"error": "", "exit_code": 0, "report_url": "", "device": ""}
    """
    logger.info(f"🚀 开始为设备 {device_serial} 启动回放任务")

    # 0. 设备状态预检查
    if not check_device_available(device_serial):
        error_msg = f"设备 {device_serial} 不可用或被占用"
        logger.error(error_msg)
        return {
            "error": error_msg,
            "exit_code": -1,
            "report_url": "",
            "device": device_serial
        }

    # 1. 构造子进程启动命令（使用动态路径避免硬编码）
    # 使用统一的脚本查找函数定位回放脚本
    script_path = find_script_path('replay_script.py')
    if not os.path.exists(script_path):
        error_msg = f"回放脚本不存在: {script_path}"
        logger.error(error_msg)
        return {
            "error": error_msg,
            "exit_code": -1,
            "report_url": "",
            "device": device_serial
        }

    # 🔧 关键修复：确保 script_args 被正确地展开到 cmd 列表中
    # 之前的问题是 script_args 被当作一个单独的元素添加
    cmd = [sys.executable, script_path, '--log-dir', log_dir, '--device', device_serial, '--multi-device']

    # 添加账号信息参数 - 告诉子进程使用预分配的账号文件
    cmd.append('--use-preassigned-accounts')

    cmd.extend(script_args)  # 使用 extend 正确展开参数列表

    # 设备独立子目录（不在这里创建，延迟到回放脚本内部创建，避免启动阻塞）
    device_dir = os.path.join(log_dir, device_serial)
    # 不提前创建: os.makedirs(device_dir, exist_ok=True)
    # 仍然按照约定路径计算预期的日志与结果文件路径（供后续等待与读取）
    device_log_file = os.path.join(device_dir, f"{device_serial}.log")  # 回放脚本内部若创建则可读取
    result_file = os.path.join(device_dir, f"{device_serial}.result.json")

    # 🔧 重要调试：详细记录命令构造过程
    logger.info(f"🔧 设备 {device_serial} 命令构造详情:")
    logger.info(f"   Python解释器: {sys.executable}")
    logger.info(f"   脚本路径: {script_path}")
    logger.info(f"   日志目录: {log_dir}")
    logger.info(f"   设备序列号: {device_serial}")
    logger.info(f"   脚本参数: {script_args}")
    logger.info(f"   脚本参数类型: {type(script_args)}")
    logger.info(f"   脚本参数长度: {len(script_args)}")

    logger.info(f"🔧 设备 {device_serial} 完整执行命令:")

    # 打印完整命令
    # logger.info(f"🔧 设备 {device_serial} 完整执行命令:")
    # for i, arg in enumerate(cmd):
    #     logger.info(f"   cmd[{i}]: {arg}")
    logger.info(f"🔧 设备 {device_serial} 日志文件: {device_log_file}")
    logger.info(f"🔧 设备 {device_serial} 结果文件: {result_file}")
    logger.info("🔧 ==========================================")

    # 重试机制
    for attempt in range(max_retries + 1):
        log_file_handle = None  # 保留变量名以兼容 finally 清理逻辑
        proc = None
        captured_output = ""  # 用于替代原文件日志的内存缓冲
        try:
            logger.info(f"设备 {device_serial} 开始第 {attempt + 1} 次尝试")

            # 2. 启动子进程，关键：直接重定向到文件避免二进制内容问题
            creation_flags = 0
            preexec_fn = None
            if sys.platform == "win32":
                creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                preexec_fn = os.setsid            # 🔧 修复：使用更安全的子进程启动方式，先验证参数
            logger.info(f"🔧 设备 {device_serial} 启动前验证:")
            logger.info(f"   工作目录: {os.getcwd()}")
            logger.info(f"   Python可执行: {os.path.exists(sys.executable)}")
            logger.info(f"   脚本文件存在: {os.path.exists(script_path)}")
            logger.info(f"   日志目录存在: {os.path.exists(log_dir)}")
            logger.info(f"   设备目录存在: {os.path.exists(device_dir)}")

            # 使用管道捕获输出，避免启动阶段的文件 IO 开销
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                creationflags=creation_flags,
                preexec_fn=preexec_fn,
                stdin=subprocess.DEVNULL,
                cwd=None,
                env=None
            )

            logger.info(f"设备 {device_serial} 子进程已启动，PID: {proc.pid}")

            # 3. 等待子进程结束，使用分阶段超时处理
            try:
                proc.wait(timeout=timeout)
                logger.info(f"设备 {device_serial} 子进程已结束，退出码: {proc.returncode}")

                # 捕获子进程标准输出内容
                try:
                    if proc.stdout:
                        raw_bytes = proc.stdout.read()  # 读取全部缓冲
                        captured_output = raw_bytes.decode('utf-8', errors='replace') if raw_bytes else ""
                except Exception as cap_e:
                    logger.warning(f"读取子进程输出失败: {cap_e}")

                # 处理日志行（内存）
                log_lines = captured_output.strip().split('\n') if captured_output else []
                last_lines = log_lines[-10:] if len(log_lines) > 10 else log_lines
                logger.info(f"🔍 设备 {device_serial} 捕获输出最后10行:")
                for i, line in enumerate(last_lines, 1):
                    logger.info(f"   [{i:2d}] {line}")

                # 检查是否有明显的错误信息
                error_indicators = ['error', 'exception', 'traceback', 'failed', '错误', '异常', '失败']
                for line in reversed(log_lines):
                    if any(indicator.lower() in line.lower() for indicator in error_indicators):
                        logger.error(f"🚨 设备 {device_serial} 发现错误信息(内存输出): {line}")
                        break

            except subprocess.TimeoutExpired:
                logger.warning(f"设备 {device_serial} 执行超时，尝试优雅终止")
                # 优雅终止：先尝试发送终止信号，给进程保存数据的机会
                if sys.platform == "win32":
                    proc.send_signal(signal.CTRL_BREAK_EVENT)
                    try:
                        proc.wait(timeout=10)  # 给10秒优雅退出时间
                    except subprocess.TimeoutExpired:
                        proc.kill()  # 强制终止
                else:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                return {
                    "error": "执行超时",
                    "exit_code": -1,
                    "report_url": "",
                    "device": device_serial
                }            # 4. 等待结果文件写入完成（避免竞争条件）
            result_wait_timeout = 30
            result_start_time = time.time()
            logger.info(f"设备 {device_serial} 等待结果文件写入: {result_file}")

            while not os.path.exists(result_file) and (time.time() - result_start_time) < result_wait_timeout:
                time.sleep(0.5)

            # 5. 读取并返回 result.json 的内容
            if os.path.exists(result_file):
                logger.info(f"🔍 设备 {device_serial} 找到结果文件: {result_file}")
                try:
                    # 多次尝试读取，确保文件写入完整
                    for read_attempt in range(3):
                        try:
                            with open(result_file, 'r', encoding='utf-8') as f:
                                content = f.read().strip()
                                logger.info(f"🔍 设备 {device_serial} 结果文件内容长度: {len(content)}")
                                logger.info(f"🔍 设备 {device_serial} 结果文件前200字符: {content[:200]}")
                                if content:  # 确保文件不为空
                                    result_data = json.loads(content)
                                    logger.info(f"🔍 设备 {device_serial} 解析后的结果数据: {result_data}")
                                    # 确保返回数据包含设备信息
                                    result_data["device"] = device_serial
                                    logger.info(f"✅ 设备 {device_serial} 执行完成，退出码: {result_data.get('exit_code', 'unknown')}")
                                    return result_data
                                else:
                                    logger.warning(f"⚠️ 设备 {device_serial} 结果文件为空，尝试 {read_attempt + 1}/3")
                        except (json.JSONDecodeError, ValueError) as e:
                            logger.warning(f"❌ 设备 {device_serial} 结果文件解析失败，尝试 {read_attempt + 1}/3: {e}")
                            if read_attempt < 2:
                                time.sleep(1)  # 等待文件写入完成
                            else:
                                raise

                    # 如果所有尝试都失败，检查日志文件获取更多信息
                    error_details = ""
                    if log_lines:
                        error_details = '\n'.join(log_lines[-5:])
                    elif os.path.exists(device_log_file):  # 兜底尝试文件（如果脚本自行创建了）
                        try:
                            with open(device_log_file, 'r', encoding='utf-8', errors='replace') as f:
                                fallback_content = f.read()
                                fb_lines = fallback_content.strip().split('\n')
                                error_details = '\n'.join(fb_lines[-5:]) if fb_lines else "日志文件为空"
                        except Exception as e:
                            error_details = f"无法读取日志文件: {e}"

                    return {
                        "error": f"结果文件为空或格式错误。日志详情: {error_details}",
                        "exit_code": proc.returncode,
                        "report_url": "",
                        "device": device_serial
                    }
                except (json.JSONDecodeError, ValueError) as e:
                    if attempt < max_retries:
                        logger.warning(f"设备 {device_serial} 结果文件格式错误，重试 {attempt + 1}/{max_retries}: {e}")
                        time.sleep(2)  # 重试前等待
                        continue
                    return {
                        "error": f"结果文件格式错误: {str(e)}",
                        "exit_code": proc.returncode,
                        "report_url": "",
                        "device": device_serial
                    }
            else:
                logger.error(f"设备 {device_serial} 未找到结果文件: {result_file}")
                # 🔧 增强：更详细的错误诊断
                error_details = ""
                script_execution_status = ""

                if log_lines:
                    # 查找关键信息（来自内存）
                    key_lines = []
                    for line in log_lines:
                        if any(keyword in line.lower() for keyword in [
                            'error', 'exception', 'traceback', 'failed', 'success',
                            'script', 'device', 'exit', 'complete', '错误', '异常', '失败', '成功'
                        ]):
                            key_lines.append(line)
                    if key_lines:
                        script_execution_status = f"\n关键执行信息:\n" + '\n'.join(key_lines[-5:])
                    error_details = '\n'.join(log_lines[-15:]) if log_lines else "日志为空"
                elif os.path.exists(device_log_file):
                    # 兜底读取文件（若脚本内部创建了）
                    try:
                        with open(device_log_file, 'r', encoding='utf-8', errors='replace') as f:
                            fallback_content = f.read()
                            fb_lines = fallback_content.strip().split('\n')
                            error_details = '\n'.join(fb_lines[-15:]) if fb_lines else "日志文件为空"
                    except Exception as e:
                        error_details = f"无法读取日志文件: {e}"
                else:
                    error_details = "日志不可用"

                full_error_msg = f"未找到结果文件，子进程退出码: {proc.returncode}。{script_execution_status}\n\n最后15行日志:\n{error_details}"

                if attempt < max_retries:
                    logger.warning(f"设备 {device_serial} 未找到结果文件，重试 {attempt + 1}/{max_retries}。错误详情: {full_error_msg}")
                    time.sleep(2)
                    continue

                return {
                    "error": full_error_msg,
                    "exit_code": proc.returncode,
                    "report_url": "",
                    "device": device_serial
                }

        except Exception as e:
            if attempt < max_retries:
                logger.warning(f"设备 {device_serial} 执行异常，重试 {attempt + 1}/{max_retries}: {e}")
                time.sleep(2)
                continue
            logger.error(f"设备 {device_serial} 执行失败: {e}")
            return {
                "error": f"执行异常: {str(e)}",
                "exit_code": -1,
                "report_url": "",
                "device": device_serial
            }
        finally:
            # 确保资源清理
            if log_file_handle:
                try:
                    log_file_handle.close()
                except:
                    pass
            if proc and proc.poll() is None:
                try:
                    if sys.platform == "win32":
                        proc.kill()
                    else:
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except:
                    pass


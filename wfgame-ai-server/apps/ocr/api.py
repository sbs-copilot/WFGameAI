"""
OCR模块API视图
"""

import csv
import json
import logging
import os
import re
import tarfile
import uuid
import zipfile

from django.http import HttpResponse
from django.conf import settings
from django.utils import timezone
from rest_framework import status
from rest_framework.views import APIView
from rest_framework.permissions import AllowAny
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
from django.db.models import Q
from utils.orm_helper import DecimalEncoder
from .models import OCRProject, OCRGitRepository, OCRTask, OCRResult, OCRCache
from .serializers import (
    OCRProjectSerializer,
    OCRGitRepositorySerializer,
    OCRTaskSerializer,
    OCRResultSerializer,
    FileUploadSerializer,
    OCRTaskCreateSerializer,
    OCRHistoryQuerySerializer,
    OCRTaskWithResultsSerializer,
    OCRProcessGitSerializer,
)
from .services.ocr_service import OCRService
from .services.gitlab import create_gitlab_service, GitLabService, GitLabConfig
from .tasks import process_ocr_task, binding_translated_image, export_offline_html_task
from .services.path_utils import PathUtils
from apps.core.utils.response import api_response
from django.db import transaction
from .services.export_service import _export_helper_xlsx
from .services.compare_service import TransRepoConfig

# 配置日志
logger = logging.getLogger(__name__)


class OCRProjectAPIView(APIView):
    """OCR项目API"""

    permission_classes = [AllowAny]
    http_method_names = ["post"]

    def post(self, request):
        """处理项目相关操作"""
        action = request.data.get("action", "")

        if action == "list":
            projects = OCRProject.objects.all()
            serializer = OCRProjectSerializer(projects, many=True)
            return api_response(serializer.data)

        elif action == "create":
            serializer = OCRProjectSerializer(data=request.data)
            if serializer.is_valid():
                serializer.save()
                return api_response(serializer.data)
            return api_response(status.HTTP_400_BAD_REQUEST, data=serializer.errors, msg="创建失败")

        elif action == "get":
            project_id = request.data.get("id")
            try:
                project = OCRProject.objects.get(id=project_id)
                serializer = OCRProjectSerializer(project)
                return api_response(serializer.data)
            except OCRProject.DoesNotExist:
                return api_response(status.HTTP_404_NOT_FOUND, msg="项目不存在")

        elif action == "delete":
            project_id = request.data.get("id")
            try:
                project = OCRProject.objects.get(id=project_id)
                project.delete()
                return api_response()
            except OCRProject.DoesNotExist:
                return api_response(code=status.HTTP_404_NOT_FOUND, msg="项目不存在")

        return api_response(code=status.HTTP_400_BAD_REQUEST, msg=f"不支持的操作: {action}")


class OCRGitRepositoryAPIView(APIView):
    """OCR Git仓库API"""

    permission_classes = [AllowAny]
    http_method_names = ["post"]

    def post(self, request):
        """处理Git仓库相关操作"""
        action = request.data.get("action", "")

        if action == "list":
            project_id = request.data.get("project_id")
            if project_id:
                repositories = OCRGitRepository.objects.filter(project_id=project_id)
            else:
                repositories = OCRGitRepository.objects.all()

            serializer = OCRGitRepositorySerializer(repositories, many=True)
            return api_response(data=serializer.data)

        elif action == "create":
            try:
                serializer = OCRGitRepositorySerializer(data=request.data)
                if serializer.is_valid():
                    # 获取访问令牌（如果提供）
                    token = request.data.get("token")

                    # 获取是否跳过SSL验证的标志
                    skip_ssl_verify = request.data.get("skip_ssl_verify", False)
                    if skip_ssl_verify:
                        logger.warning("⚠️ 用户请求跳过SSL验证，这可能存在安全风险")

                    # 验证Git仓库URL
                    url = serializer.validated_data.get("url")
                    try:
                        is_valid = create_gitlab_service(
                            url, token=token, skip_ssl_verify=skip_ssl_verify
                        ).validate_repository()
                        if not is_valid:
                            return api_response(
                                code=status.HTTP_400_BAD_REQUEST,
                                msg="Git仓库URL无效或无法访问"
                            )
                    except Exception as e:
                        logger.error(f"验证仓库URL失败: {str(e)}")
                        return api_response(
                            code=status.HTTP_400_BAD_REQUEST,
                            msg=f"验证仓库URL失败: {str(e)}"
                        )

                    # 保存仓库信息，但不存储令牌
                    serializer.save()

                    return api_response(data=serializer.data, msg="创建成功")
                return api_response(code=status.HTTP_400_BAD_REQUEST, data=serializer.errors, msg="创建失败")
            except Exception as e:
                logger.error(f"创建仓库异常: {str(e)}")
                return api_response(
                    code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    msg=f"创建仓库失败: {str(e)}"
                )

        elif action == "get":
            repo_id = request.data.get("id")
            try:
                repo = OCRGitRepository.objects.get(id=repo_id)
                serializer = OCRGitRepositorySerializer(repo)
                return api_response(data=serializer.data)
            except OCRGitRepository.DoesNotExist:
                return api_response(
                    code=status.HTTP_404_NOT_FOUND,
                    msg="Git仓库不存在"
                )

        elif action == "delete":
            repo_id = request.data.get("id")
            try:
                repo = OCRGitRepository.objects.get(id=repo_id)
                repo.delete()
                return api_response(msg="Git仓库删除成功")
            except OCRGitRepository.DoesNotExist:
                return api_response(
                    code=status.HTTP_404_NOT_FOUND,
                    msg="Git仓库不存在"
                )

        elif action == "get_branches":
            repo_id = request.data.get("id")
            try:
                repo = OCRGitRepository.objects.get(id=repo_id)
                # 获取是否跳过SSL验证的标志
                skip_ssl_verify = request.data.get("skip_ssl_verify", False)
                if skip_ssl_verify:
                    logger.warning("⚠️ 用户请求跳过SSL验证获取分支，这可能存在安全风险")

                branches = create_gitlab_service(
                    repo.url, token=repo.token, skip_ssl_verify=skip_ssl_verify
                ).get_repository_branches()
                return api_response(data={"branches": branches})
            except OCRGitRepository.DoesNotExist:
                return api_response(
                    code=status.HTTP_404_NOT_FOUND,
                    msg="Git仓库不存在"
                )
            except Exception as e:
                return api_response(
                    code=status.HTTP_400_BAD_REQUEST,
                    msg=f"获取分支失败: {str(e)}"
                )

        return api_response(
            code=status.HTTP_400_BAD_REQUEST,
            msg=f"不支持的操作: {action}"
        )


class OCRTaskAPIView(APIView):
    """OCR任务API"""

    permission_classes = [AllowAny]
    http_method_names = ["post"]

    def post(self, request):
        """处理任务相关操作"""
        action = request.data.get("action", "")

        if action == "list":
            tasks = OCRTask.objects.all().order_by("-created_time")
            serializer = OCRTaskSerializer(tasks, many=True)
            return api_response(data=serializer.data)

        elif action == "create":
            serializer = OCRTaskCreateSerializer(data=request.data)
            if serializer.is_valid():
                # 创建任务
                task = serializer.save()

                # 使用事务确保任务创建完成后再提交Celery任务
                from django.db import transaction
                import time
                
                def submit_celery_task():
                    time.sleep(0.1)  # 短暂延迟确保事务提交
                    logger.info(f"提交OCR任务到Celery: {task.id}")
                    process_ocr_task.delay(task.id)
                
                transaction.on_commit(submit_celery_task)

                result_serializer = OCRTaskSerializer(task)
                return api_response(data=result_serializer.data, msg="任务创建成功")
            return api_response(code=status.HTTP_400_BAD_REQUEST, data=serializer.errors, msg="任务创建失败")

        elif action == "get":
            task_id = request.data.get("id")
            try:
                task = OCRTask.objects.get(id=task_id)
                serializer = OCRTaskSerializer(task)

                # # 获取实时进度数据
                # key = f'ai_ocr_progress:{task.id}'
                # progress_data = settings.REDIS.hgetall(key)
                #
                # total_images = int(progress_data.get('total', 0))
                # matched_images = int(progress_data.get('matched', 0))
                # executed_images = int(progress_data.get('executed', 0))
                # match_rate = (matched_images / total_images * 100) if total_images > 0 else 0
                # # 前 10% 用于准备阶段，后 90% 用于处理阶段
                # font_default_progress = 10.0 # 默认进度10%
                # progress = int(progress_data.get('executed', 0)) / total_images * 100 * 0.9 + font_default_progress if total_images > 0 else font_default_progress
                # response_data = serializer.data
                # response_data.update({
                #     'status': progress_data.get('status', ''),
                #     'total_images': total_images,
                #     'matched_images': matched_images,
                #     'executed_images': executed_images,
                #     'match_rate': round(match_rate, 2),
                #     'progress': round(progress, 2),
                #     'start_time': float(progress_data.get('start_time', 0)),
                #     'end_time': float(progress_data.get('end_time', 0))
                # })
                #
                # # 汇总报告
                # report_file = task.config.get('report_file')
                # if report_file and os.path.exists(report_file):
                #     with open(report_file, 'r', encoding='utf-8') as f:
                #         report_content = f.read()
                #     response_data['report'] = {
                #         'content': report_content,
                #         'file_path': report_file
                #     }

                return api_response(data=serializer.data)
            except OCRTask.DoesNotExist:
                return api_response(
                    code=status.HTTP_404_NOT_FOUND,
                    msg="任务不存在"
                )
        # elif action == "get":
        #     task_id = request.data.get("id")
        #     try:
        #         task = OCRTask.objects.get(id=task_id)
        #         serializer = OCRTaskSerializer(task)
        #
        #         # 获取实时进度数据
        #         key = f'ai_ocr_progress:{task.id}'
        #         progress_data = settings.REDIS.hgetall(key)
        #
        #         # 字段映射与模型同步
        #         total_images = int(progress_data.get('total', 0))
        #         matched_images = int(progress_data.get('matched', 0))
        #         match_rate = (matched_images / total_images * 100) if total_images > 0 else 0
        #
        #         response_data = serializer.data
        #         response_data.update({
        #             'status': progress_data.get('status', ''),
        #             'total_images': total_images,
        #             'matched_images': matched_images,
        #             'match_rate': round(match_rate, 2),
        #             'start_time': float(progress_data.get('start_time', 0)),
        #             'end_time': float(progress_data.get('end_time', 0)),
        #             'executed_images': int(progress_data.get('executed', 0)),
        #             'success_images': int(progress_data.get('success', 0)),
        #             'fail_images': int(progress_data.get('fail', 0)),
        #             'exception_images': int(progress_data.get('exception', 0)),
        #         })
        #
        #         # 检查是否有汇总报告
        #         report_file = task.config.get('report_file')
        #         if report_file and os.path.exists(report_file):
        #             with open(report_file, 'r', encoding='utf-8') as f:
        #                 report_content = f.read()
        #             response_data['report'] = {
        #                 'content': report_content,
        #                 'file_path': report_file
        #             }
        #
        #         return Response(response_data)
        #     except OCRTask.DoesNotExist:
        #         return Response(
        #             {"detail": "任务不存在"}, status=status.HTTP_404_NOT_FOUND
        #         )

        elif action == "delete":
            task_id = request.data.get("id")
            try:
                task = OCRTask.objects.get(id=task_id)
                task.delete()
                return api_response(msg="任务删除成功")
            except OCRTask.DoesNotExist:
                return api_response(
                    code=status.HTTP_404_NOT_FOUND,
                    msg="任务不存在"
                )

        elif action == "get_details":
            task_id = request.data.get("id")
            has_math = request.data.get("has_match", None)
            keyword = request.data.get("keyword", "")
            result_type = request.data.get("result_type")
            min_confidence = request.data.get("min_confidence", 0)
            max_confidence = request.data.get("max_confidence", 1)
            is_verified = request.data.get("is_verified", None)
            is_translated = request.data.get("is_translated", None)

            try:
                task = OCRTask.objects.get(id=task_id)
                results = task.related_results

                if str(result_type).isdigit():
                    results = results.filter(result_type=result_type)

                if has_math is not None:
                    results = results.filter(has_match=has_math)

                if is_translated is not None:
                    results = results.filter(is_translated=is_translated)

                if is_verified is not None:
                    results = results.filter(is_verified=is_verified)

                if keyword != "":
                    regex = f"/[^/]*{keyword}[^/]*$"
                    results = results.filter(image_path__iregex=regex)

                if min_confidence is not None and min_confidence != "":
                    results = results.filter(max_confidence__gte=min_confidence)

                if max_confidence is not None and max_confidence != "":
                    results = results.filter(max_confidence__lte=max_confidence)

                # 获取分页参数
                page = int(request.data.get("page", 1))
                page_size = int(request.data.get("page_size", 20))

                # 分页
                start = (page - 1) * page_size
                end = start + page_size

                # 获取当前页的结果
                paginated_results = results[start:end]

                # 序列化
                # task_serializer = OCRTaskSerializer(task)
                results_serializer = OCRResultSerializer(paginated_results, many=True)

                return api_response(data={
                        # "task": task_serializer.data,
                        "results": results_serializer.data,
                        "total": results.count(),
                        "page": page,
                        "page_size": page_size,
                        "total_pages": (results.count() + page_size - 1) // page_size,
                    })

            except OCRTask.DoesNotExist:
                return api_response(
                    code=status.HTTP_404_NOT_FOUND,
                    msg="任务不存在"
                )

        elif action == "export":
            task_id = request.data.get("id")
            export_format = request.data.get("format", "json")

            try:
                task = OCRTask.objects.get(id=task_id)
                results = OCRResult.objects.filter(task=task)
                print("results : ", results)
                if export_format == "json":
                    # 导出为JSON
                    export_data = []
                    for result in results:
                        # 确保使用相对路径
                        image_path = result.image_path
                        # 使用PathUtils规范化路径
                        image_path = PathUtils.normalize_path(image_path)

                        export_data.append(
                            {
                                "image_path": image_path,
                                "texts": result.texts,
                                "languages": result.languages,
                                "has_match": result.has_match,
                                "processing_time": result.processing_time,
                            }
                        )

                    # 创建响应
                    response = HttpResponse(
                        json.dumps(export_data, ensure_ascii=False, indent=2, cls=DecimalEncoder),
                        content_type="application/json",
                    )
                    response["Content-Disposition"] = (
                        f"attachment; filename=ocr_task_{task_id}.json"
                    )
                    return response

                elif export_format == "csv":
                    # 直接导出 helper 风格的 xlsx（替换旧逻辑，不再保留CSV旧格式）
                    xbytes, fname = _export_helper_xlsx(request, results, str(task_id))
                    response = HttpResponse(xbytes, content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                    response["Content-Disposition"] = f"attachment; filename={fname}"
                    return response

                elif export_format == "txt":
                    # 导出为TXT
                    from io import StringIO

                    txt_buffer = StringIO()

                    # 写入数据
                    for result in results:
                        # 确保使用相对路径
                        image_path = result.image_path
                        # 使用PathUtils规范化路径
                        image_path = PathUtils.normalize_path(image_path)

                        txt_buffer.write(f"图片路径: {image_path}\n")
                        txt_buffer.write(f"识别文本:\n")
                        for i, text in enumerate(result.texts, 1):
                            txt_buffer.write(f"{i}. {text}\n")
                        txt_buffer.write(
                            f"语言: {', '.join(result.languages.keys())}\n"
                        )
                        txt_buffer.write(
                            f"匹配: {'是' if result.has_match else '否'}\n"
                        )
                        txt_buffer.write("-" * 50 + "\n\n")

                    # 创建响应
                    response = HttpResponse(
                        txt_buffer.getvalue(), content_type="text/plain"
                    )
                    response["Content-Disposition"] = (
                        f"attachment; filename=ocr_task_{task_id}.txt"
                    )
                    return response

                else:
                    return api_response(
                        code=status.HTTP_400_BAD_REQUEST,
                        msg=f"不支持的导出格式: {export_format}"
                    )

            except OCRTask.DoesNotExist:
                return api_response(
                    code=status.HTTP_404_NOT_FOUND,
                    msg="任务不存在"
                )

        elif action == "get_source_dirs":
            task_id = request.data.get("task_id")
            path = request.data.get("path", "")
            try:
                task = OCRTask.objects.get(id=task_id)
                target_dir = task.config.get('target_dir')
                target_path = task.config.get('target_path')
                
                if not target_dir or not target_path:
                    return api_response(code=status.HTTP_400_BAD_REQUEST, msg="任务配置缺失 target_dir 或 target_path")
                
                base_path = os.path.join(target_dir, target_path)
                full_search_path = os.path.join(base_path, path)
                
                if not os.path.exists(full_search_path):
                     return api_response(code=status.HTTP_404_NOT_FOUND, msg=f"目录不存在: {full_search_path}")

                nodes = []
                with os.scandir(full_search_path) as it:
                    for entry in it:
                        if entry.is_dir():
                            rel_path = os.path.join(path, entry.name).replace('\\', '/')
                            nodes.append({
                                "label": entry.name,
                                "value": rel_path,
                                "leaf": False
                            })
                
                nodes.sort(key=lambda x: x['label'])
                return api_response(data=nodes)
            except OCRTask.DoesNotExist:
                return api_response(code=status.HTTP_404_NOT_FOUND, msg="任务不存在")

        elif action == "get_trans_repo_dirs":
            repo_id = request.data.get("repo_id")
            branch = request.data.get("branch")
            path = request.data.get("path", "")
            
            try:
                repo = OCRGitRepository.objects.get(id=repo_id)
                
                git_config = GitLabConfig(
                    repo_url=repo.url,
                    access_token=repo.token,
                )
                git_service = GitLabService(config=git_config)
                
                # 获取仓库目录结构
                dirs = git_service.get_repository_directories(branch=branch, path=path)
                
                return api_response(data=dirs)
                
            except OCRGitRepository.DoesNotExist:
                return api_response(code=status.HTTP_404_NOT_FOUND, msg="仓库不存在")
            except Exception as e:
                logger.error(f"获取仓库目录失败: {e}")
                return api_response(code=status.HTTP_500_INTERNAL_SERVER_ERROR, msg=f"获取目录失败: {str(e)}")

        elif action == "bind_trans_repo":
            task_id = request.data.get("task_id")
            repo_id = request.data.get("repo_id")
            branch = request.data.get("branch")
            mapping = request.data.get("mapping")
            
            try:
                task = OCRTask.objects.get(id=task_id)
                repo = OCRGitRepository.objects.get(id=repo_id)
                
                if not task.config:
                    task.config = {}

                # 翻译仓库目录名：仓库名_分支名
                repo_name = GitLabService(
                    GitLabConfig(
                        repo_url=repo.url,
                        access_token=repo.token,
                    )).get_repo_name(repo.url)
                target_path = f"{repo_name}_{branch}"
                
                # 构建配置
                trans_repo_config = {
                    "url": repo.url,
                    "branch": branch,
                    "access_token": repo.token,
                    "target_dir": PathUtils.get_ocr_repos_dir(),
                    "target_path": target_path,
                    "mapping": mapping,
                    # 结果状态
                    "status": "binding",  # binding, completed, failed
                    "error": "",
                }
                
                # 简单的校验
                try:
                    TransRepoConfig(**trans_repo_config)
                except Exception as e:
                    return api_response(code=status.HTTP_400_BAD_REQUEST, msg=f"配置格式错误: {str(e)}")

                task.config['trans_repo'] = trans_repo_config
                task.save()
                
                binding_translated_image.delay(task.id)
                
                return api_response(msg="关联翻译资源任务已提交")
                
            except OCRTask.DoesNotExist:
                return api_response(code=status.HTTP_404_NOT_FOUND, msg="任务不存在")
            except OCRGitRepository.DoesNotExist:
                return api_response(code=status.HTTP_404_NOT_FOUND, msg="翻译仓库不存在")

        elif action == "unbind_trans_repo":
            task_id = request.data.get("task_id")
            if not task_id:
                return api_response(code=status.HTTP_400_BAD_REQUEST, msg="缺少task_id参数")
            
            try:
                task = OCRTask.objects.get(id=task_id)
                config = task.config or {}
                if 'trans_repo' in config:
                    del config['trans_repo']
                    task.config = config
                    task.save()

                # 清空结果关联
                task.related_results.update(
                    is_translated=False,
                    trans_image_path=""
                )
                return api_response()
            except OCRTask.DoesNotExist:
                return api_response(code=status.HTTP_404_NOT_FOUND, msg="任务不存在")

        elif action == "export_offline_html":
            task_id = request.data.get("task_id")
            if not task_id:
                return api_response(code=status.HTTP_400_BAD_REQUEST, msg="缺少task_id参数")
            
            # 检查文件是否已存在（可选优化）
            report_dir = PathUtils.get_ocr_reports_dir()
            file_name = f"ocr_report_{task_id}_offline.html"
            file_path = os.path.join(report_dir, file_name)
            if os.path.exists(file_path):
                return api_response(data={"url": f"ocr/reports/{file_name}"})

            # 触发异步任务
            export_offline_html_task.delay(task_id)
            
            # 返回预期的下载地址
            return api_response(data={
                "url": f"ocr/reports/{file_name}",
                "msg": "正在生成离线报告，请稍后下载"
            })

        return api_response(
            code=status.HTTP_400_BAD_REQUEST,
            msg=f"不支持的操作: {action}"
        )


class OCRResultAPIView(APIView):
    """OCR结果API"""

    permission_classes = [AllowAny]
    http_method_names = ["post"]

    def post(self, request):
        """处理结果相关操作"""
        action = request.data.get("action", "")

        if action == "list":
            task_id = request.data.get("task_id")
            if task_id:
                results = OCRResult.objects.filter(task_id=task_id)
            else:
                results = OCRResult.objects.all()

            serializer = OCRResultSerializer(results, many=True)
            return api_response(data=serializer.data)

        elif action == "get":
            result_id = request.data.get("id")
            try:
                result = OCRResult.objects.get(id=result_id)
                serializer = OCRResultSerializer(result)
                return api_response(data=serializer.data)
            except OCRResult.DoesNotExist:
                return api_response(
                    code=status.HTTP_404_NOT_FOUND,
                    msg="结果不存在"
                )

        elif action == "search":
            # 搜索结果
            task_id = request.data.get("task_id")
            query = request.data.get("query", "")
            only_matched = request.data.get("only_matched", False)

            if not task_id:
                return api_response(
                    code=status.HTTP_400_BAD_REQUEST,
                    msg="缺少task_id参数"
                )

            try:
                results = OCRResult.objects.filter(task_id=task_id)

                if only_matched:
                    results = results.filter(has_match=True)

                if query:
                    # 在文本内容中搜索
                    matching_results = []
                    for result in results:
                        for text in result.texts:
                            if query.lower() in text.lower():
                                matching_results.append(result)
                                break

                    # 转换为QuerySet
                    result_ids = [r.id for r in matching_results]
                    results = OCRResult.objects.filter(id__in=result_ids)

                # 获取分页参数
                page = int(request.data.get("page", 1))
                page_size = int(request.data.get("page_size", 20))

                # 分页
                start = (page - 1) * page_size
                end = start + page_size

                # 获取当前页的结果
                paginated_results = results[start:end]

                # 序列化
                serializer = OCRResultSerializer(paginated_results, many=True)

                return api_response(data={
                        "results": serializer.data,
                        "total": results.count(),
                        "page": page,
                        "page_size": page_size,
                        "total_pages": (results.count() + page_size - 1) // page_size,
                    })

            except Exception as e:
                return api_response(
                    code=status.HTTP_400_BAD_REQUEST,
                    msg=f"搜索失败: {str(e)}"
                )

        elif action == "update":
            # 更新结果处理是否正确，result_type
            result_ids = request.data.get("ids", {})
            # 拼接更新结构体，包括ids， result_type

            update_results = []
            with transaction.atomic():
                for id, result_type in result_ids.items():
                    try:
                        result = OCRResult.objects.get(id=id)
                        result.result_type = result_type
                        update_results.append(result)
                        OCRCache.set_ground_truth(result.image_hash, result.id)
                    except OCRResult.DoesNotExist:
                        continue

                if not update_results:
                    return api_response(
                        code=status.HTTP_400_BAD_REQUEST,
                        msg="没有有效的结果需要更新"
                    )

                # 批量更新sql
                OCRResult.objects.bulk_update(update_results, ['result_type'])

            return api_response(
                msg="结果更新成功", data={"total": len(update_results)}
            )

        elif action == "verify":
            # 人工校验逻辑
            result_id = request.data.get("id")
            task_id = request.data.get("task_id")
            result_type = request.data.get("result_type")
            corrected_texts = request.data.get("corrected_texts")

            if not task_id:
                return api_response(
                    code=status.HTTP_400_BAD_REQUEST,
                    msg="缺少task_id参数"
                )

            item = OCRResult.objects.filter(id=result_id).first()
            if not item:
                return api_response(
                    code=status.HTTP_404_NOT_FOUND,
                    msg=f"OCR结果（id:{result_id}）不存在"
                )
            # 如果标注为非正确，则必须传递纠正文本
            if result_type > 1:
                if not corrected_texts:
                    return api_response(
                        code=status.HTTP_400_BAD_REQUEST,
                        msg="标注为非正确结果时，必须提供纠正后的文本"
                    )

            item.verify(task_id, result_type, corrected_texts)
            return api_response()

        elif action == "batch_verify_right":
            # 批量标注为正确结果
            task_id = request.data.get("task_id")
            ids = request.data.get("ids", [])
            if not task_id:
                return api_response(
                    code=status.HTTP_400_BAD_REQUEST,
                    msg="缺少task_id参数"
                )
            if not ids:
                return api_response(
                    code=status.HTTP_400_BAD_REQUEST,
                    msg="缺少ids参数"
                )
            OCRResult.batch_verify(
                task_id,
            [
                {
                    "id": i,
                    "result_type": 1,
                    "corrected_texts": []
                }
                for i in ids
            ])
            return api_response()

        return api_response(
            code=status.HTTP_400_BAD_REQUEST,
            msg=f"不支持的操作: {action}"
        )


class OCRUploadAPIView(APIView):
    """文件上传API"""

    permission_classes = [AllowAny]
    parser_classes = [MultiPartParser, FormParser, JSONParser]
    http_method_names = ["post"]

    def post(self, request):
        # 处理文件上传
        logger.info(f"收到文件上传请求，数据类型: {type(request.data)}")
        logger.info(f"请求数据键: {list(request.data.keys())}")
        
        serializer = FileUploadSerializer(data=request.data)
        if not serializer.is_valid():
            logger.error(f"参数验证失败: {serializer.errors}")
            return api_response(code=status.HTTP_400_BAD_REQUEST, data=serializer.errors, msg="参数验证失败")

        uploaded_file = serializer.validated_data.get("file")
        project_id = serializer.validated_data.get("project_id")
        languages = serializer.validated_data.get("languages", ["ch"])
        enable_cache = serializer.validated_data.get("enable_cache", True)
        keyword_filter = serializer.validated_data.get("keyword_filter", {})

        try:
            # 确保项目存在
            try:
                project = OCRProject.objects.get(id=project_id)
            except OCRProject.DoesNotExist:
                return api_response(
                    code=status.HTTP_404_NOT_FOUND,
                    msg="项目不存在"
                )

            # 创建上传ID：格式为 upload_YYYYMMDD_HHMMSS_项目名_随机码
            from datetime import datetime
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            # random_code = uuid.uuid4().hex[:6]
            # 清理项目名称，移除特殊字符
            safe_project_name = re.sub(r'[^\w\-]', '_', project.name)[:20]
            # upload_id = f"upload_{timestamp}_{safe_project_name}_{random_code}"
            upload_id = f"upload_{timestamp}_{safe_project_name}"
            
            uploads_base_dir = PathUtils.get_ocr_uploads_dir()
            upload_dir = os.path.join(uploads_base_dir, upload_id)
            
            # 调试信息
            logger.info(f"上传基础目录: {uploads_base_dir}")
            logger.info(f"完整上传目录: {upload_dir}")
            
            os.makedirs(upload_dir, exist_ok=True)
            logger.info(f"目录创建成功: {upload_dir}")

            # 保存文件
            file_path = os.path.join(upload_dir, uploaded_file.name)
            with open(file_path, "wb+") as destination:
                for chunk in uploaded_file.chunks():
                    destination.write(chunk)

            # 处理压缩文件
            if file_path.endswith(".zip"):
                self._extract_zip_file(file_path, upload_dir)
                os.remove(file_path)  # 删除原始ZIP文件
            elif file_path.endswith(".tar.gz") or file_path.endswith(".tgz"):
                self._extract_tar_file(file_path, upload_dir)
                os.remove(file_path)  # 删除原始TAR文件

            # 创建OCR任务
            # 计算相对于MEDIA_ROOT的相对路径
            abs_upload_dir = os.path.abspath(upload_dir)
            abs_media_root = os.path.abspath(settings.MEDIA_ROOT)
            if abs_upload_dir.startswith(abs_media_root):
                relative_upload_dir = os.path.relpath(abs_upload_dir, abs_media_root).replace('\\', '/')
            else:
                # 降级方案：如果路径不在media下，只保存upload_id
                relative_upload_dir = upload_id
            logger.info(f"相对上传目录: {relative_upload_dir}")
            task = OCRTask.objects.create(
                project=project,
                source_type="upload",
                name=f"上传识别_{uploaded_file.name}",
                status="pending",
                config={
                    "target_languages": languages,
                    "upload_id": upload_id,
                    "target_dir": relative_upload_dir,
                    "enable_cache": enable_cache,
                    "keyword_filter": keyword_filter,  # 关键字过滤配置
                },
            )

            # 确保数据库事务提交后再提交Celery任务
            from django.db import transaction
            import time
            
            logger.info(f"创建OCR任务成功，任务ID: {task.id}, 类型: {type(task.id)}")
            logger.info(f"任务详细信息: 名称={task.name}, 状态={task.status}, 创建时间={task.created_at}")
            
            # 使用事务确保任务创建完成后再提交Celery任务
            def submit_celery_task():
                # 添加短暂延迟确保数据库事务完全提交
                time.sleep(0.1)
                
                # 验证任务是否存在
                verification_task = OCRTask.objects.filter(id=task.id).first()
                if verification_task:
                    logger.info(f"任务验证通过，提交异步Celery任务: {task.id}")
                    # 使用apply_async指定任务ID，确保Celery任务ID与数据库任务ID一致
                    celery_result = process_ocr_task.apply_async(args=[task.id], task_id=f"task-{task.id}")
                    logger.info(f"Celery任务已提交: {task.id}, Celery任务ID: {celery_result.id}")
                else:
                    logger.error(f"任务验证失败，任务不存在: {task.id}")
                    # 记录详细调试信息
                    all_tasks = OCRTask.objects.values_list('id', 'name', 'status')[:5]
                    logger.error(f"数据库中的任务示例: {list(all_tasks)}")
            
            # 在事务提交后执行Celery任务提交
            transaction.on_commit(submit_celery_task)
            # process_ocr_task(task.id)

            serializer = OCRTaskSerializer(task)
            return api_response(
                data={"task": serializer.data},
                msg="文件上传成功，开始OCR处理"
            )

        except Exception as e:
            return api_response(
                code=status.HTTP_400_BAD_REQUEST,
                msg=f"文件上传处理失败: {str(e)}"
            )

    def _extract_zip_file(self, zip_path, extract_to):
        """解压ZIP文件"""
        with zipfile.ZipFile(zip_path, "r") as zip_ref:
            zip_ref.extractall(extract_to)
        logger.info(f"解压缩ZIP文件到 {extract_to}")

    def _extract_tar_file(self, tar_path, extract_to):
        """解压TAR文件"""
        with tarfile.open(tar_path, "r:*") as tar_ref:
            tar_ref.extractall(path=extract_to)
        logger.info(f"解压缩TAR文件到 {extract_to}")


class OCRProcessAPIView(APIView):
    """OCR处理API"""

    permission_classes = [AllowAny]
    http_method_names = ["post"]

    def post(self, request):
        """处理OCR请求"""
        action = request.data.get("action", "")
        if action == "process_git":
            # 处理Git仓库源OCR
            logger.info(f"收到Git OCR请求，原始数据: {request.data}")
            serializer = OCRProcessGitSerializer(data=request.data)
            if serializer.is_valid():
                # 获取参数
                project_id = serializer.validated_data.get("project_id")
                repo_id = serializer.validated_data.get("repo_id")
                branch = serializer.validated_data.get("branch", "main")
                languages = serializer.validated_data.get("languages", ["ch"])
                enable_cache = serializer.validated_data.get("enable_cache", True)
                keyword_filter = serializer.validated_data.get("keyword_filter", {})
                model_path = serializer.validated_data.get("model_path", "")
                rec_score_thresh = serializer.validated_data.get("rec_score_thresh", 0.5)

                try:
                    # 获取项目和仓库
                    # project = OCRProject.objects.get(id=project_id)
                    git_repo = OCRGitRepository.objects.get(id=repo_id)
                    # 创建OCR任务
                    task = OCRTask.objects.create(
                        # project=project,
                        git_repository=git_repo,
                        git_branch=branch,
                        source_type="git",
                        status="pending",
                        config={
                            "branch": branch,
                            "target_languages": languages,  # 统一使用target_languages字段
                            "target_dir": PathUtils.get_ocr_repos_dir(),
                            # 项目代码所在目录，与 target_dir 相对路径
                            "target_path": GitLabService(
                                GitLabConfig(repo_url=git_repo.url,
                                             access_token=git_repo.token,
                                             )).get_repo_name(git_repo.url),
                            "enable_cache": enable_cache,
                            "keyword_filter": keyword_filter,  # 关键字过滤配置
                            "model_path": model_path,
                            "rec_score_thresh": rec_score_thresh,
                        },
                    )

                    # 使用事务确保任务创建完成后再提交Celery任务
                    from django.db import transaction
                    import time
                    
                    logger.info(f"创建Git OCR任务成功，任务ID: {task.id}, 类型: {type(task.id)}")
                    logger.info(f"Git任务详细信息: 名称={task.name}, 状态={task.status}, 创建时间={task.created_at}")
                    
                    def submit_celery_task():
                        time.sleep(0.3)  # 增加延迟到500ms，确保数据库事务完全提交
                        
                        # 验证任务是否存在
                        verification_task = OCRTask.objects.filter(id=task.id).first()
                        if verification_task:
                            logger.info(f"Git任务验证通过，提交异步Celery任务: {task.id}")
                            # 使用apply_async指定任务ID，确保Celery任务ID与数据库任务ID一致
                            celery_result = process_ocr_task.apply_async(args=[task.id], task_id=f"task-{task.id}")
                            logger.info(f"Git Celery任务已提交: {task.id}, Celery任务ID: {celery_result.id}")
                        else:
                            logger.error(f"Git任务验证失败，任务不存在: {task.id}")
                            # 记录详细调试信息
                            all_tasks = OCRTask.objects.values_list('id', 'name', 'status')[:5]
                            logger.error(f"数据库中的任务示例: {list(all_tasks)}")
                    
                    transaction.on_commit(submit_celery_task)
                    
                    # 返回任务信息
                    serializer = OCRTaskSerializer(task)
                    return api_response(data=serializer.data, msg="Git仓库OCR任务创建成功")

                except OCRProject.DoesNotExist:
                    return api_response(
                        code=status.HTTP_404_NOT_FOUND,
                        msg="项目不存在"
                    )
                except OCRGitRepository.DoesNotExist:
                    return api_response(
                        code=status.HTTP_404_NOT_FOUND,
                        msg="仓库不存在"
                    )
                except Exception as e:
                    return api_response(
                        code=status.HTTP_400_BAD_REQUEST,
                        msg=f"处理失败: {str(e)}"
                    )

            return api_response(code=status.HTTP_400_BAD_REQUEST, data=serializer.errors, msg="参数验证失败")

        return api_response(
            code=status.HTTP_400_BAD_REQUEST,
            msg=f"不支持的操作: {action}"
        )


class OCRHistoryAPIView(APIView):
    """OCR历史记录API"""

    permission_classes = [AllowAny]
    http_method_names = ["post"]

    def post(self, request):
        """查询历史记录"""
        action = request.data.get("action", "list")

        if action == "download":
            return self.download_results(request)

        if action == "del":
            return self.del_result(request)
        # 默认list操作
        serializer = OCRHistoryQuerySerializer(data=request.data)
        if not serializer.is_valid():
            return api_response(code=status.HTTP_400_BAD_REQUEST, data=serializer.errors, msg="参数验证失败")

        # 获取查询参数
        project_id = serializer.validated_data.get("project_id")
        date_from = serializer.validated_data.get("date_from")
        date_to = serializer.validated_data.get("date_to")
        page = serializer.validated_data.get("page", 1)
        page_size = serializer.validated_data.get("page_size", 20)

        # 构建查询
        query = Q()

        if project_id:
            query &= Q(project_id=project_id)

        if date_from:
            query &= Q(created_at__gte=date_from)

        if date_to:
            # 设置日期结束时间为当天23:59:59
            date_to = date_to.replace(hour=23, minute=59, second=59)
            query &= Q(created_at__lte=date_to)

        # 执行查询
        tasks = OCRTask.objects.filter(query).order_by("-created_at")

        # 计算总数
        total_count = tasks.count()

        # 分页
        start = (page - 1) * page_size
        end = start + page_size
        paginated_tasks = tasks[start:end]

        # 序列化
        task_serializer = OCRTaskWithResultsSerializer(paginated_tasks, many=True)

        return api_response(data={
                "tasks": task_serializer.data,
                "total": total_count,
                "page": page,
                "page_size": page_size,
                "total_pages": (total_count + page_size - 1) // page_size,
            })

    def download_results(self, request):
        """下载任务结果为CSV"""
        task_id = request.data.get("task_id")
        if not task_id:
            return api_response(
                code=status.HTTP_400_BAD_REQUEST,
                msg="缺少task_id参数"
            )

        try:
            # 获取任务信息
            task = OCRTask.objects.get(id=task_id)

            # 获取该任务的所有结果
            results = task.related_results

            # 直接导出 helper 风格的 xlsx（替换旧CSV导出逻辑）
            xbytes, fname = _export_helper_xlsx(request, results, str(task_id), task_name=(task.name or str(task.id)))
            response = HttpResponse(xbytes, content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            response["Access-Control-Expose-Headers"] = "Content-Disposition"
            response["Content-Disposition"] = f"attachment; filename={fname}"
            return response

        except OCRTask.DoesNotExist:
            return api_response(
                code=status.HTTP_404_NOT_FOUND,
                msg="任务不存在"
            )
        except Exception as e:
            return api_response(
                code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                msg=f"下载失败: {str(e)}"
            )

    def del_result(self, request):
        """删除历史任务及其结果"""
        task_id = request.data.get("task_id")
        if not task_id:
            return api_response(
                code=status.HTTP_400_BAD_REQUEST,
                msg="缺少task_id参数"
            )

        try:
            # 删除 task 表数据
            task = OCRTask.objects.get(id=task_id)
            task.delete()

            # 删除redis key数据
            settings.REDIS.delete(f'ai_ocr_progress:{task_id}')
            return api_response()

        except OCRTask.DoesNotExist:
            return api_response(
                code=status.HTTP_404_NOT_FOUND,
                msg="任务不存在"
            )
        except Exception as e:
            return api_response(
                code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                msg=f"删除失败: {str(e)}"
            )


class OCRCacheStatusView(APIView):
    """OCR缓存状态查看API"""
    
    permission_classes = [AllowAny]
    http_method_names = ['post']
    
    def post(self, request):
        """获取OCR缓存池状态"""
        try:
            # 创建OCRService实例来访问缓存池
            ocr_service = OCRService()
            
            # 获取缓存统计信息
            cache_info = ocr_service.ocr_pool.get_cache_info()
            
            return api_response(data={
                "cache_info": cache_info,
                "timestamp": timezone.now()
            })
            
        except Exception as e:
            logger.error(f"获取OCR缓存状态失败: {e}")
            return api_response(
                code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                msg=f"获取缓存状态失败: {str(e)}"
            )


class OCRCacheClearView(APIView):
    """OCR缓存清理API"""
    
    permission_classes = [AllowAny]
    http_method_names = ['post']
    
    def post(self, request):
        """清空OCR缓存池"""
        try:
            # 创建OCRService实例来访问缓存池
            ocr_service = OCRService()
            
            # 清空缓存
            ocr_service.ocr_pool.clear_cache()
            
            return api_response(data={
                "timestamp": timezone.now()
            })
            
        except Exception as e:
            logger.error(f"清理OCR缓存失败: {e}")
            return api_response(
                code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                msg=f"清理缓存失败: {str(e)}"
            )

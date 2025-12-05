"""
OCR模块序列化器
"""

from rest_framework import serializers
from django.utils import timezone
from .models import OCRProject, OCRGitRepository, OCRTask, OCRResult


class OCRProjectSerializer(serializers.ModelSerializer):
    """OCR项目序列化器"""

    class Meta:
        model = OCRProject
        fields = ["id", "name", "description", "created_at", "updated_at"]


class OCRGitRepositorySerializer(serializers.ModelSerializer):
    """OCR Git仓库序列化器"""

    class Meta:
        model = OCRGitRepository
        fields = "__all__"


class OCRTaskSerializer(serializers.ModelSerializer):
    """OCR任务序列化器"""

    project_name = serializers.ReadOnlyField(source="project.name")
    git_repository_url = serializers.ReadOnlyField(
        source="git_repository.url", default=""
    )
    duration = serializers.SerializerMethodField()

    class Meta:
        model = OCRTask
        fields = "__all__"

    def get_duration(self, obj):
        """计算任务执行时长"""
        if obj.start_time and obj.end_time:
            duration = obj.end_time - obj.start_time
            return str(duration).split(".")[0]  # 格式化为 HH:MM:SS
        return "00:00:00"


class OCRResultSerializer(serializers.ModelSerializer):
    """OCR结果序列化器"""

    task_id = serializers.ReadOnlyField(source="task.id")

    class Meta:
        model = OCRResult
        fields = [
            "id",
            "task",
            "task_id",
            "image_hash",
            "image_path",
            "is_translated",
            "trans_image_path",
            "texts",
            "corrected_texts",
            "languages",
            "has_match",
            "max_confidence",
            "processing_time",
            "created_at",
            "result_type",
            "pic_resolution",
            "is_verified"
        ]


class OCRProcessGitSerializer(serializers.Serializer):
    """Git仓库处理序列化器"""

    project_id = serializers.IntegerField(required=True)
    repo_id = serializers.IntegerField(required=True)
    branch = serializers.CharField(required=True)
    languages = serializers.ListField(
        child=serializers.CharField(), required=False, default=["ch"]
    )
    token = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    enable_cache = serializers.BooleanField(required=False, default=True)
    keyword_filter = serializers.DictField(required=False, default=dict)
    rec_score_thresh = serializers.FloatField(required=False, default=0.5)
    model_path = serializers.CharField(required=True, allow_blank=False, allow_null=False)

    def validate(self, data):
        """验证处理参数"""
        if not data.get("project_id"):
            raise serializers.ValidationError("项目ID为必填项")
        if not data.get("repo_id"):
            raise serializers.ValidationError("仓库ID为必填项")
        
        # 验证关键字过滤配置
        keyword_filter = data.get("keyword_filter", {})
        if keyword_filter.get("enabled") and not keyword_filter.get("keywords"):
            raise serializers.ValidationError("启用关键字过滤时必须提供关键字")
        
        return data


class TaskCreationSerializer(serializers.Serializer):
    """任务创建序列化器"""

    project_id = serializers.IntegerField(required=True)
    source_type = serializers.ChoiceField(choices=["git", "upload"], required=True)
    git_repository_id = serializers.IntegerField(required=False, allow_null=True)
    branch = serializers.CharField(required=False, allow_blank=True, default="main")
    image_formats = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        default=["jpg", "jpeg", "png", "bmp", "tiff", "tif", "webp"],
    )
    target_languages = serializers.ListField(
        child=serializers.CharField(), required=False, default=["ch"]
    )

    def validate(self, data):
        """验证任务创建参数"""
        if data["source_type"] == "git" and not data.get("git_repository_id"):
            raise serializers.ValidationError("Git仓库ID为必填项")
        return data


class FileUploadSerializer(serializers.Serializer):
    """文件上传序列化器"""

    file = serializers.FileField(required=True)
    project_id = serializers.IntegerField(required=True)
    languages = serializers.CharField(required=False, default='["ch"]')
    enable_cache = serializers.BooleanField(required=False, default=True)
    keyword_filter = serializers.CharField(required=False, default='{}')
    
    def validate_keyword_filter(self, value):
        """验证并解析keyword_filter字段"""
        try:
            import json
            if isinstance(value, str):
                filter_dict = json.loads(value)
            elif isinstance(value, dict):
                filter_dict = value
            else:
                return {}
            
            # 验证关键字过滤配置
            if filter_dict.get('enabled') and not filter_dict.get('keywords'):
                raise serializers.ValidationError("启用关键字过滤时必须提供关键字")
            
            return filter_dict
        except json.JSONDecodeError:
            raise serializers.ValidationError("keyword_filter格式错误，必须是有效的JSON对象")
    
    def validate_languages(self, value):
        """验证并解析languages字段"""
        try:
            import json
            if isinstance(value, str):
                languages_list = json.loads(value)
            else:
                languages_list = value
            
            if not isinstance(languages_list, list):
                raise serializers.ValidationError("languages必须是一个列表")
            
            if not languages_list:
                return ["ch"]  # 默认值
                
            return languages_list
        except json.JSONDecodeError:
            raise serializers.ValidationError("languages格式错误，必须是有效的JSON数组")

    def validate_file(self, value):
        """验证上传文件"""
        # 文件大小限制 (100MB)
        if value.size > 100 * 1024 * 1024:
            raise serializers.ValidationError("文件大小不能超过100MB")

        # 验证文件扩展名
        allowed_extensions = [
            ".jpg",
            ".jpeg",
            ".png",
            ".bmp",
            ".tiff",
            ".tif",
            ".webp",
            ".zip",
            ".tar.gz",
            ".tgz",
        ]
        ext = value.name.lower()
        if not any(ext.endswith(allowed_ext) for allowed_ext in allowed_extensions):
            raise serializers.ValidationError(
                f"不支持的文件类型，允许的格式: {', '.join(allowed_extensions)}"
            )

        return value


class OCRTaskCreateSerializer(serializers.ModelSerializer):
    """OCR任务创建序列化器"""

    class Meta:
        model = OCRTask
        fields = ["id", "project", "git_repository", "source_type", "status", "config"]
        read_only_fields = ["id", "status"]

    def create(self, validated_data):
        """创建任务"""
        # 设置任务状态为待处理
        validated_data["status"] = "pending"

        # 不再需要处理 name 字段，因为它已从模型中移除
        return super().create(validated_data)


class OCRHistoryQuerySerializer(serializers.Serializer):
    """OCR历史查询序列化器"""

    project_id = serializers.IntegerField(required=False, allow_null=True)
    date_from = serializers.DateTimeField(required=False, allow_null=True)
    date_to = serializers.DateTimeField(required=False, allow_null=True)
    page = serializers.IntegerField(required=False, default=1)
    page_size = serializers.IntegerField(required=False, default=20)


class OCRTaskWithResultsSerializer(serializers.ModelSerializer):
    """带有结果统计的OCR任务序列化器"""

    project_name = serializers.ReadOnlyField(source="project.name")
    git_repository_url = serializers.ReadOnlyField(
        source="git_repository.url", default=""
    )
    duration = serializers.SerializerMethodField()
    results_count = serializers.SerializerMethodField()

    class Meta:
        model = OCRTask
        # 排除字段
        exclude = ["name"]

    def get_duration(self, obj):
        """计算任务执行时长"""
        if obj.start_time and obj.end_time:
            duration = obj.end_time - obj.start_time
            return str(duration).split(".")[0]  # 格式化为 HH:MM:SS
        return "00:00:00"

    def get_results_count(self, obj):
        """获取结果数量"""
        return OCRResult.objects.filter(task=obj).count()


class OCRPerformanceSerializer(serializers.Serializer):
    """OCR性能统计序列化器"""

    gpu_usage = serializers.FloatField()
    cpu_usage = serializers.FloatField()
    memory_usage = serializers.FloatField()
    active_tasks = serializers.IntegerField()
    pending_tasks = serializers.IntegerField()
    completed_tasks = serializers.IntegerField()
    failed_tasks = serializers.IntegerField()
    average_processing_time = serializers.FloatField()

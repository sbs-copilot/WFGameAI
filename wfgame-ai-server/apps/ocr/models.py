"""
OCR模块数据模型
"""
import difflib

from django.db import models
from django.db.models import Q
import datetime

from django.db.models import QuerySet

from apps.core.models.common import CommonFieldsMixin
from django.db import transaction


def generate_task_id():
    """生成任务ID"""
    # 使用YYYY-MM-dd_HH-MM-SS-微秒格式生成任务ID，确保唯一性
    now = datetime.datetime.now()
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H-%M-%S")
    microsecond_str = f"{now.microsecond:06d}"[:3]  # 取前3位微秒，避免ID过长
    return f"task_{date_str}_{time_str}-{microsecond_str}"

# deprecated 系统根据 team_id 自动管理数据隔离
class OCRProject(models.Model):
    """OCR项目模型"""

    name = models.CharField(max_length=100, verbose_name="项目名称")
    description = models.TextField(blank=True, null=True, verbose_name="项目描述")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")

    def __str__(self):
        return self.name

    class Meta:
        verbose_name = "OCR项目"
        verbose_name_plural = "OCR项目"
        db_table = "ocr_project"


class OCRGitRepository(CommonFieldsMixin):
    """OCR Git仓库模型"""

    project = models.ForeignKey(
        OCRProject,
        on_delete=models.CASCADE,
        related_name="repositories",
        verbose_name="所属项目",
        null=True,
    )
    url = models.CharField(max_length=255, verbose_name="仓库URL")
    branch = models.CharField(max_length=100, default="main", verbose_name="默认分支")
    token = models.CharField(max_length=255, default="", verbose_name="访问令牌")

    def __str__(self):
        return f"{self.url} ({self.branch})"

    class Meta:
        verbose_name = "OCR Git仓库"
        verbose_name_plural = "OCR Git仓库"
        db_table = "ocr_git_repository"


class OCRTask(CommonFieldsMixin):
    """OCR任务模型"""

    STATUS_CHOICES = (
        ("pending", "等待中"),
        ("running", "执行中"),
        ("completed", "已完成"),
        ("failed", "失败"),
    )
    SOURCE_CHOICES = (("git", "Git仓库"), ("upload", "本地上传"))

    id = models.CharField(primary_key=True, max_length=50, default=generate_task_id)
    name = models.CharField(
        max_length=255, verbose_name="任务名称", blank=True, null=True
    )
    project = models.ForeignKey(
        OCRProject,
        on_delete=models.CASCADE,
        related_name="tasks",
        verbose_name="所属项目",
        null=True
    )
    source_type = models.CharField(
        max_length=10, choices=SOURCE_CHOICES, verbose_name="来源类型"
    )
    git_repository = models.ForeignKey(
        OCRGitRepository,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name="Git仓库",
    )
    git_branch = models.CharField(
        max_length=255, default="main", verbose_name="Git分支", blank=True, null=True
    )
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default="pending",
        verbose_name="任务状态",
    )
    config = models.JSONField(verbose_name="任务配置")
    start_time = models.DateTimeField(null=True, blank=True, verbose_name="开始时间")
    end_time = models.DateTimeField(null=True, blank=True, verbose_name="结束时间")
    total_images = models.IntegerField(default=0, verbose_name="总图片数")
    verified_images = models.IntegerField(default=0, verbose_name="已校验图片数")
    processed_images = models.IntegerField(default=0, verbose_name="已处理图片数")
    matched_images = models.IntegerField(default=0, verbose_name="匹配图片数")
    match_rate = models.DecimalField(
        max_digits=5, decimal_places=2, default=0.00, verbose_name="匹配率"
    )

    def __str__(self):
        return self.id

    class Meta:
        verbose_name = "OCR任务"
        verbose_name_plural = "OCR任务"
        db_table = "ocr_task"

    @property
    def related_results(self) -> QuerySet:
        """获取关联的OCR结果列表"""
        # 修复：使用正确的外键关系查询
        query = Q(task=self)
        has_cache = OCRCacheHit.objects.filter(task_id=self.id).first()
        if has_cache:
            cached_result_ids = [int(i) for i in has_cache.result_ids.split(',')]
            query = query | Q(id__in=cached_result_ids)
        return OCRResult.objects.all_teams().filter(query).order_by("id")

    @property
    def total_verified(self) -> int:
        """获取已校验图片数"""
        return self.related_results.filter(is_verified=True).count()

    def update_verified_count(self):
        """更新已校验图片数"""
        self.verified_images = self.total_verified
        self.save(update_fields=["verified_images"])

    def calculate_match_rate_by_related_results(self):
        """通过 related_results 计算匹配率"""
        import logging
        from django.db import connection
        logger = logging.getLogger(__name__)
        
        # 强制刷新数据库连接，确保能看到最新数据
        connection.ensure_connection()
        
        # 先尝试直接查询OCRResult，看看是否能找到数据
        from apps.ocr.models import OCRResult
        direct_results = OCRResult.objects.filter(task=self)
        direct_count = direct_results.count()
        logger.warning(f"任务 {self.id} 直接查询OCRResult: 找到 {direct_count} 条结果")
        
        # 如果直接查询有结果，打印一些调试信息
        if direct_count > 0:
            first_result = direct_results.first()
            logger.warning(f"任务 {self.id} 第一条结果: task_id={first_result.task_id if hasattr(first_result, 'task_id') else 'N/A'}, task.id={first_result.task.id}")
        
        # 再使用related_results查询
        related_results = self.related_results
        total = related_results.count()
        logger.warning(f"任务 {self.id} related_results查询: 查询到 {total} 条相关结果")
        
        if total == 0:
            matched_images = 0
            match_rate = 0.00
            logger.warning(f"任务 {self.id} 统计计算: 未找到相关结果")
        else:
            matched_images = related_results.filter(has_match=True).count()
            match_rate = round((matched_images / total) * 100, 2)
            logger.warning(f"任务 {self.id} 统计计算: 总数={total}, 匹配数={matched_images}, 匹配率={match_rate}%")
        
        # 更新任务统计字段
        old_values = (self.processed_images, self.matched_images, self.match_rate)
        self.processed_images = total
        self.matched_images = matched_images
        self.match_rate = match_rate
        self.save(update_fields=["processed_images", "matched_images", "match_rate"])
        
        logger.warning(f"任务 {self.id} 统计更新: {old_values} -> ({total}, {matched_images}, {match_rate})")

    def auto_label_with_ground_truth(self):
        """
        自动标注功能：
        遍历当前任务的所有结果，根据 image_hash 去 OCRCache 查找是否存在真值。
        如果存在真值，计算文本相似度，并自动标记结果类型。
        """
        # 1. 获取当前任务的所有结果
        current_results = self.results.all()
        
        # 2. 批量获取涉及的 image_hash 对应的缓存 (只取已人工校验的真值)
        hashes = [r.image_hash for r in current_results]
        caches = OCRCache.objects.filter(
            image_hash__in=hashes, 
            is_verified=True
        )
        
        # 构建哈希映射: image_hash -> ground_truth_result_id
        truth_map = {c.image_hash: c.result_id for c in caches}
        
        if not truth_map:
            return

        # 3. 批量获取真值对应的 OCRResult 内容
        truth_result_ids = list(truth_map.values())
        truth_results = OCRResult.objects.in_bulk(truth_result_ids)
        
        updates = []
        
        for result in current_results:
            if result.image_hash not in truth_map:
                continue
                
            truth_id = truth_map[result.image_hash]
            truth_obj = truth_results.get(truth_id)
            
            if not truth_obj:
                continue

            # 清洗数据：转字符串，去空格，忽略切分差异
            text_new = "".join(str(x) for x in result.texts).replace(" ", "")
            text_truth = "".join(str(x) for x in truth_obj.texts).replace(" ", "")
            
            # 计算相似度 (SequenceMatcher ratio)
            seq = difflib.SequenceMatcher(None, text_new, text_truth)
            score = seq.ratio()
            
            result.similarity_score = score
            result.ground_truth_origin_id = truth_id
            
            updates.append(result)
            
        if updates:
            OCRResult.objects.bulk_update(updates, fields=['similarity_score', 'ground_truth_origin_id'])



class OCRResult(CommonFieldsMixin):
    """OCR结果模型"""
    TYPE_CHOICES = (
        (1, "正确"),    # 完全正确
        (2, "错误"),    # 合并错误类型
    )

    task = models.ForeignKey(
        OCRTask,
        on_delete=models.CASCADE,
        related_name="results",
        verbose_name="所属任务",
        null=True,
    )
    image_hash = models.CharField(max_length=128, default="", verbose_name="图片哈希值")
    image_path = models.CharField(max_length=255, verbose_name="图片路径")
    is_translated = models.BooleanField(default=False, null=True, verbose_name="是否已翻译")
    trans_image_path = models.CharField(max_length=255, blank=True, null=True, verbose_name="翻译后图片路径")
    texts = models.JSONField(verbose_name="识别文本")
    languages = models.JSONField(null=True, blank=True, verbose_name="识别语言")
    has_match = models.BooleanField(default=False, verbose_name="是否匹配")
    max_confidence = models.DecimalField(
        max_digits=5, decimal_places=4, null=True, blank=True, verbose_name="最高识别分数", default=0.0
    )
    confidences = models.JSONField(null=True, blank=True, verbose_name="各文本置信度列表", default=list)
    processing_time = models.DecimalField(
        max_digits=10, decimal_places=3, null=True, blank=True, verbose_name="处理时间"
    )
    result_type = models.IntegerField(
        choices=TYPE_CHOICES, default=1, verbose_name="识别结果类型"
    )
    is_verified = models.BooleanField(default=False, verbose_name="是否人工校验")
    pic_resolution = models.CharField(max_length=50, blank=True, null=True, verbose_name="图片分辨率")
    # 人工矫正的文本内容
    corrected_texts = models.JSONField(null=True, blank=True, verbose_name="人工矫正文本", default=list)
    # 自动标注时结果参考的原始 result_id
    ground_truth_origin_id = models.BigIntegerField(null=True, blank=True, verbose_name="对比真值来源ID")
    # 自动标注时的相似度分数
    similarity_score = models.DecimalField(
        max_digits=5, decimal_places=4, default=0.0, verbose_name="真值相似度"
    )
    corrected_origin_id = models.BigIntegerField(null=True, db_index=True, blank=True, verbose_name="人工矫正来源ID")

    def __str__(self):
        task_id = self.task.id if self.task else "None"
        return f"{task_id}_{self.id}"

    class Meta:
        verbose_name = "OCR结果"
        verbose_name_plural = "OCR结果"
        db_table = "ocr_result"


    def verify(self, task_id:str, result_type: int, corrected_texts: list = None):
        """
        人工校验本次结果（代理到 batch_verify）
        """
        self.batch_verify(
            task_id,
            [{
            'id': self.id,
            'result_type': result_type,
            'corrected_texts': corrected_texts
        }])

    @classmethod
    def batch_verify(cls, task_id:str, verify_data_list: list):
        """
        批量校验方法，优化数据库操作性能
        由于缓存设计的存在，批量操作的 OcrResult 不一定全属于同一个任务，而有可能是历史 OcrResult
        所以需要传递当前操作的 task_id 参数，以更新当前任务的已校验数量
        :param task_id: str, OCR任务ID
        :param verify_data_list: list[dict], 每一项包含 {'id': int, 'result_type': int, 'corrected_texts': list}
        """
        if not verify_data_list:
            return

        # 提取 ID 和数据映射
        # 过滤掉没有 id 的无效数据
        data_map = {str(item['id']): item for item in verify_data_list if 'id' in item}
        ids = list(data_map.keys())

        if not ids:
            return

        # 1. 批量获取当前结果对象
        # 注意：id 是字符串还是数字取决于模型定义，这里假设模型定义兼容
        current_results = cls.objects.in_bulk(ids)

        # 2. 预取可能存在的真值副本 (corrected_origin_id 在这批 ID 中)
        existing_copies = cls.objects.filter(corrected_origin_id__in=ids)
        existing_copies_map = {str(copy.corrected_origin_id): copy for copy in existing_copies}

        results_to_update = []
        copies_to_update = []
        copies_to_create = []

        # 用于后续更新 Cache 的映射: image_hash -> ground_truth_result_id
        cache_update_map = {}

        # 临时存储需要创建副本的 origin_id，用于后续反查 ID
        origins_needing_copy = []

        with transaction.atomic():
            for original_id, result in current_results.items():

                # original_id 在 in_bulk 返回字典中通常是主键类型
                str_id = str(original_id)
                if str_id not in data_map:
                    continue

                data = data_map[str_id]
                result_type = data.get('result_type')
                corrected_texts = data.get('corrected_texts', [])
                if corrected_texts is None:
                    corrected_texts = []

                # 更新原始记录
                result.is_verified = True
                result.result_type = result_type

                if result_type == 1:
                    # 正确
                    result.corrected_texts = []
                    cache_update_map[result.image_hash] = result.id
                    result.has_match = any(result.texts or [])
                else:
                    # 误检/漏检
                    result.corrected_texts = corrected_texts
                    result.has_match = any(result.corrected_texts or [])

                    # 处理副本
                    if str_id in existing_copies_map:
                        # 更新现有副本
                        copy = existing_copies_map[str_id]
                        copy.texts = corrected_texts
                        copy.result_type = 1
                        copy.is_verified = True
                        copy.corrected_texts = []
                        copy.has_match = True
                        copies_to_update.append(copy)
                        cache_update_map[result.image_hash] = copy.id
                    else:
                        # 准备创建新副本
                        # 复制必要字段
                        new_copy = cls(
                            texts=corrected_texts,
                            result_type=1,
                            is_verified=True,
                            corrected_texts=[],
                            has_match=True,
                            task=None,
                            corrected_origin_id=result.id,
                            image_hash=result.image_hash,
                            image_path=result.image_path,
                            pic_resolution=result.pic_resolution,
                            languages=result.languages,
                            max_confidence=1.0, # 人工修正默认为最高置信度
                            confidences=[],
                            processing_time=0,
                        )
                        copies_to_create.append(new_copy)
                        origins_needing_copy.append(result.id)

                results_to_update.append(result)

            # 3. 执行批量更新/插入
            if results_to_update:
                cls.objects.bulk_update(results_to_update, ['is_verified', 'result_type', 'corrected_texts', 'has_match'])

            if copies_to_update:
                cls.objects.bulk_update(copies_to_update, ['texts', 'is_verified', 'result_type', 'corrected_texts', 'has_match'])

            if copies_to_create:
                cls.objects.bulk_create(copies_to_create)

                # 4. 反查新创建的副本 ID 以更新 Cache
                new_copies = cls.objects.filter(corrected_origin_id__in=origins_needing_copy)
                for copy in new_copies:
                    cache_update_map[copy.image_hash] = copy.id

            # 5. 批量更新 Cache
            if cache_update_map:
                OCRCache.batch_set_ground_truth(cache_update_map)

        # 6. 更新任务的已校验数量
        task = OCRTask.objects.filter(id=task_id).first()
        if task:
            task.update_verified_count()

class OCRCache(models.Model):
    """
    OCR图片缓存表
    记录已识别图片的哈希值及其对应的首次识别结果ID，避免重复识别相同图片
    以提升OCR功能响应速度，仅缓存首次识别成功的结果，后续相同图片均复用该结果
    """
    image_hash = models.CharField(primary_key=True, max_length=128, verbose_name="图片MD5哈希")
    # 新增字段：标记该缓存是否经过人工确认（无论确认结果是正确、误检还是漏检）
    is_verified = models.BooleanField(default=False, verbose_name="是否人工校验")
    result_id = models.BigIntegerField(db_index=True, verbose_name="首次识别结果ID")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")

    class Meta:
        verbose_name = "OCR图片缓存"
        verbose_name_plural = "OCR图片缓存"
        db_table = "ocr_cache"

    @staticmethod
    def hash_set():
        """获取所有已缓存的图片哈希集合"""
        return set(OCRCache.objects.values_list("image_hash", flat=True))

    @staticmethod
    def record_cache(task_id: str):
        """
        记录OCR任务的缓存结果
        逻辑升级：
        1. 如果缓存不存在 -> 创建 (is_verified=False)
        2. 如果缓存存在且 is_verified=False -> 更新 (假设最新跑的任务结果更优)
        3. 如果缓存存在且 is_verified=True -> **跳过更新** (保护人工真值)
        """
        results = OCRResult.objects.all_teams().filter(task_id=task_id).only("image_hash", "id")

        # 当前已保存的缓存记录(字典形式,方便后续查询)
        existing_caches = {
            cache.image_hash: cache
            for cache in OCRCache.objects.filter(
                image_hash__in=[r.image_hash for r in results]
            )
        }

        update_caches = []
        create_caches = []

        for result in results:
            if result.image_hash in existing_caches:
                cache = existing_caches[result.image_hash]
                # 关键逻辑：只有在未人工校验的情况下，才允许机器结果覆盖缓存
                if not cache.is_verified:
                    cache.result_id = result.id
                    update_caches.append(cache)
            else:
                # 创建新的缓存记录 (默认为未校验)
                create_caches.append(
                    OCRCache(image_hash=result.image_hash, result_id=result.id, is_verified=False)
                )

        if update_caches:
            OCRCache.objects.bulk_update(update_caches, fields=["result_id"])

        if create_caches:
            OCRCache.objects.bulk_create(create_caches, ignore_conflicts=True)

    @staticmethod
    def set_ground_truth(image_hash: str, result_id: int):
        """
        人工校验接口：将某个结果标记为真值
        (内部调用 batch_set_ground_truth 实现)
        """
        OCRCache.batch_set_ground_truth({image_hash: result_id})

    @staticmethod
    def batch_set_ground_truth(hash_id_map: dict):
        """
        批量设置真值
        :param hash_id_map: {image_hash: result_id}
        """
        if not hash_id_map:
            return

        hashes = list(hash_id_map.keys())
        existing_caches = OCRCache.objects.filter(image_hash__in=hashes)
        existing_map = {c.image_hash: c for c in existing_caches}

        to_update = []
        to_create = []

        for img_hash, res_id in hash_id_map.items():
            if not img_hash or not res_id:
                continue
            if img_hash in existing_map:
                cache = existing_map[img_hash]
                # 仅当结果ID变化或未校验时才更新
                if cache.result_id != res_id or not cache.is_verified:
                    cache.result_id = res_id
                    cache.is_verified = True
                    to_update.append(cache)
            else:
                to_create.append(OCRCache(
                    image_hash=img_hash,
                    result_id=res_id,
                    is_verified=True
                ))

        if to_update:
            OCRCache.objects.bulk_update(to_update, ['result_id', 'is_verified'])
        if to_create:
            OCRCache.objects.bulk_create(to_create, ignore_conflicts=True)


class OCRCacheHit(models.Model):
    """
    OCR 缓存命中记录表
    本质上是 Task 和 Result 的多对多关系，但是为了将每次任务执行的空间复杂度降为 O(1)
    将 result_ids 的外键 concatenate 到 task_id 上，避免使用中间表带来的额外空间开销
    记录每次 OCR 任务中，哪些结果是通过缓存命中获得的
    """
    task_id = models.CharField(primary_key=True, max_length=50, verbose_name="OCR任务ID")
    result_ids = models.TextField(verbose_name="缓存命中结果ID列表，逗号分隔。例如：1,2,3")

    class Meta:
        verbose_name = "OCR缓存命中记录"
        verbose_name_plural = "OCR缓存命中记录"
        db_table = "ocr_cache_hit"

    @staticmethod
    def try_hit(image_hashes, task_id=None, batch_size=1000):
        """
        分批次查询 OCRCache，获取命中的结果ID列表。
        如果提供了 task_id，则同时创建 OCRCacheHit 记录。
        """
        hit_hashes = set()
        matched_result_ids = []

        for i in range(0, len(image_hashes), batch_size):
            batch_hashes = image_hashes[i : i + batch_size]
            cache_entries = OCRCache.objects.filter(image_hash__in=batch_hashes)
            matched_result_ids.extend(entry.result_id for entry in cache_entries)
            hit_hashes.update(entry.image_hash for entry in cache_entries)
        if task_id and matched_result_ids:
            OCRCacheHit.objects.create(
                task_id=task_id,
                result_ids=",".join(str(rid) for rid in matched_result_ids)
            )
        return hit_hashes



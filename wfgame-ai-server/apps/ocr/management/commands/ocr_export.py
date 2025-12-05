from django.core.management.base import BaseCommand
from apps.ocr.services.export_service import export_and_upload_helper_xlsx

class Command(BaseCommand):
    help = 'Export OCR task results to helper xlsx and upload to MinIO'

    def add_arguments(self, parser):
        parser.add_argument('task_id', type=str, help='The ID of the OCR task to export')

    def handle(self, *args, **options):
        task_id = options['task_id']
        self.stdout.write(f"🔔  开始导出 OCR 任务 {task_id} 的结果到 xlsx 并上传到 MinIO...")
        
        try:
            url = export_and_upload_helper_xlsx(task_id)
            
            if url:
                self.stdout.write(self.style.SUCCESS(f"✅ 导出成功！文件已上传至 MinIO，访问链接: {url}"))
            else:
                self.stdout.write(self.style.ERROR("❌ 导出失败，未获取到上传链接。"))
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"❌ 导出过程中出现错误: {str(e)}"))

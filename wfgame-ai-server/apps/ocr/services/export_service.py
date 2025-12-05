import os
import logging
import json as _json
import numpy as _np
import pandas as _pd
from io import BytesIO
from os.path import basename as _basename
from django.conf import settings

from .path_utils import PathUtils
from apps.ocr.models import OCRTask


logger = logging.getLogger(__name__)

# 在文件内新增：按helper规范导出xlsx的工具函数
def _export_helper_xlsx(request, results, task_id: str, task_name: str = ""):
    """将 OCRResult 列表导出为 helper 风格的 xlsx（多sheet）。

    返回: (bytes_io_value, filename)
    """
    # 读取首轮命中参数，填充参数列
    first_hit = {}
    try:
        report_dir = PathUtils.get_ocr_reports_dir()
        param_file = os.path.join(report_dir, f"{task_id}_first_hit.json")
        if os.path.exists(param_file):
            with open(param_file, 'r', encoding='utf-8') as fp:
                first_hit = _json.load(fp)
    except Exception:
        first_hit = {}

    # 解析分辨率字符串
    def _get_base_resolution(resolution: str):
        try:
            if 'x' in resolution:
                parts = resolution.lower().split('x')
                if len(parts) == 2:
                    w = int(parts[0].strip())
                    h = int(parts[1].strip())
                    return w, h
            return None, None
        except Exception:
            return None, None

    # 分辨率缩放计算（与helper一致）
    def _compute_scaled(w: int, h: int, limit_type: str, side_len: int) -> str:
        try:
            w = int(w)
            h = int(h)
            side = int(side_len)
            if w <= 0 or h <= 0 or side <= 0:
                return f"{w}x{h}"
            cur_max = float(max(h, w))
            cur_min = float(min(h, w))
            lt = str(limit_type)
            if lt == 'max':
                if cur_max > side:
                    r = float(side) / cur_max
                    sw = int(round(w * r))
                    sh = int(round(h * r))
                    return f"{max(1, sw)}x{max(1, sh)}"
                return f"{w}x{h}"
            if lt == 'min':
                if cur_min < side:
                    r = float(side) / cur_min
                    sw = int(round(w * r))
                    sh = int(round(h * r))
                    return f"{max(1, sw)}x{max(1, sh)}"
                return f"{w}x{h}"
            # 兼容未知取值
            denom = float(min(h, w)) if lt == 'min' else float(max(h, w))
            if denom <= 0:
                return f"{w}x{h}"
            r = float(side) / denom
            sw = int(round(w * r))
            sh = int(round(h * r))
            return f"{max(1, sw)}x{max(1, sh)}"
        except Exception:
            return f"{w}x{h}"

    # 主表 data
    rows = []
    sizes = []
    for r in results:
        img_path = PathUtils.normalize_path(r.image_path)
        img_name = _basename(img_path)
        texts = r.texts if isinstance(r.texts, list) else []
        corrected_texts = r.corrected_texts if isinstance(r.corrected_texts, list) else []
        fh = first_hit.get(img_name) or {}
        # 分辨率
        resolution_str = r.pic_resolution or ''
        # scaled
        scaled_str = fh.get('scaled_resolution', '')
        base_w, base_h = _get_base_resolution(resolution_str)

        # 尺寸与统计
        if base_w and base_h:
            sizes.append({
                'image': img_name,
                'width': int(base_w),
                'height': int(base_h),
                'min_side': int(min(base_w, base_h)),
                'max_side': int(max(base_w, base_h)),
                'area': int(base_w) * int(base_h)
            })

        if not scaled_str and base_w and base_h and fh.get('text_det_limit_type') and fh.get('text_det_limit_side_len'):
            scaled_str = _compute_scaled(base_w, base_h, str(fh.get('text_det_limit_type')),
                                         int(fh.get('text_det_limit_side_len')))
        if not scaled_str:
            scaled_str = resolution_str
        # texts/scores/num_items
        # scores_str = fh.get('hit_scores', '')
        confidences = r.confidences if isinstance(r.confidences, list) else []
        num_items_val = len(confidences)
        rows.append({
            'image_name': img_name,
            'image_path': img_path,
            'texts': texts or corrected_texts,
            'max_confidence': r.max_confidence,
            'resolution': resolution_str,
            'scaled_resolution': scaled_str,
            'num_items': num_items_val,
            'scores': confidences,
            'use_doc_orientation_classify': 'False',
            'use_doc_unwarping': str(bool(fh.get('use_doc_unwarping', False))),
            'use_textline_orientation': str(bool(fh.get('use_textline_orientation', False))),
            'text_det_limit_type': fh.get('text_det_limit_type', ''),
            'text_det_limit_side_len': fh.get('text_det_limit_side_len', ''),
            'text_det_thresh': fh.get('text_det_thresh', ''),
            'text_det_box_thresh': fh.get('text_det_box_thresh', ''),
            'text_det_unclip_ratio': fh.get('text_det_unclip_ratio', ''),
            'text_rec_score_thresh': fh.get('text_rec_score_thresh', ''),
            'hit_round': fh.get('round', ''),
        })

    df_data = _pd.DataFrame(rows)

    # 尺寸与统计
    # sizes = []
    # for p in img_paths:
    #     try:
    #         full = p if os.path.isabs(p) else os.path.join(settings.MEDIA_ROOT, p)
    #         data = _np.fromfile(full, dtype=_np.uint8)
    #         import cv2 as _cv2
    #         img = _cv2.imdecode(data, _cv2.IMREAD_COLOR)
    #         if img is not None:
    #             h, w = img.shape[:2]
    #             sizes.append({'image': _basename(p), 'width': int(w), 'height': int(h),
    #                           'min_side': int(min(w, h)), 'max_side': int(max(w, h)),
    #                           'area': int(w) * int(h)})
    #     except Exception:
    #         continue

    df_sizes = _pd.DataFrame(sizes)

    desc = _pd.DataFrame()
    pcts = _pd.Series(dtype=float)
    bin_min_counts = _pd.DataFrame()
    bin_max_counts = _pd.DataFrame()
    summary_rows = []
    if not df_sizes.empty:
        try:
            desc = df_sizes[['min_side', 'max_side', 'width', 'height', 'area']].describe()
            pcts = df_sizes['min_side'].quantile([0.5, 0.75, 0.9, 0.95, 0.99]).rename('quantile')
            if not df_sizes.empty:
                summary_rows = [
                    {'metric': 'total_images', 'value': int(df_sizes.shape[0])},
                    {'metric': 'unique_images', 'value': int(df_sizes['image'].nunique())},
                    {'metric': 'p50_min_side', 'value': float(pcts.loc[0.5]) if 0.5 in pcts.index else None},
                    {'metric': 'p75_min_side', 'value': float(pcts.loc[0.75]) if 0.75 in pcts.index else None},
                    {'metric': 'p90_min_side', 'value': float(pcts.loc[0.9]) if 0.9 in pcts.index else None},
                    {'metric': 'p95_min_side', 'value': float(pcts.loc[0.95]) if 0.95 in pcts.index else None},
                    {'metric': 'p99_min_side', 'value': float(pcts.loc[0.99]) if 0.99 in pcts.index else None},
                ]
            # 直方分布
            bins_min = [0, 80, 100, 120, 140, 160, 180, 200, 240, 320, 480, 720, 960, 1280, 1600, _np.inf]
            labels_min = ['0-80', '80-100', '100-120', '120-140', '140-160', '160-180', '180-200', '200-240', '240-320',
                          '320-480', '480-720', '720-960', '960-1280', '1280-1600', '1600+']
            df_sizes['min_side_bin'] = _pd.cut(df_sizes['min_side'], bins=bins_min, labels=labels_min, right=False)
            bin_min_counts = df_sizes['min_side_bin'].value_counts().sort_index().rename('count').reset_index().rename(
                columns={'index': 'min_side_bin'})
            bins_max = [0, 160, 200, 240, 320, 480, 640, 720, 960, 1280, 1600, 1920, 2560, 3000, 4000, _np.inf]
            labels_max = ['0-160', '160-200', '200-240', '240-320', '320-480', '480-640', '640-720', '720-960',
                          '960-1280', '1280-1600', '1600-1920', '1920-2560', '2560-3000', '3000-4000', '4000+']
            df_sizes['max_side_bin'] = _pd.cut(df_sizes['max_side'], bins=bins_max, labels=labels_max, right=False)
            bin_max_counts = df_sizes['max_side_bin'].value_counts().sort_index().rename('count').reset_index().rename(
                columns={'index': 'max_side_bin'})
        except Exception:
            pass

    # 输出xlsx到内存
    bio = BytesIO()
    try:
        with _pd.ExcelWriter(bio, engine='openpyxl') as xw:
            df_data.to_excel(xw, sheet_name='data', index=False)
            if summary_rows:
                _pd.DataFrame(summary_rows).to_excel(xw, sheet_name='summary', index=False)
            if not desc.empty:
                desc.reset_index().rename(columns={'index': 'stat'}).to_excel(xw, sheet_name='size_stats', index=False)
            if not bin_min_counts.empty:
                bin_min_counts.to_excel(xw, sheet_name='size_bins_min', index=False)
            if not bin_max_counts.empty:
                bin_max_counts.to_excel(xw, sheet_name='size_bins_max', index=False)
    except Exception:
        with _pd.ExcelWriter(bio, engine='xlsxwriter') as xw:
            df_data.to_excel(xw, sheet_name='data', index=False)
            if summary_rows:
                _pd.DataFrame(summary_rows).to_excel(xw, sheet_name='summary', index=False)
            if not desc.empty:
                desc.reset_index().rename(columns={'index': 'stat'}).to_excel(xw, sheet_name='size_stats', index=False)
            if not bin_min_counts.empty:
                bin_min_counts.to_excel(xw, sheet_name='size_bins_min', index=False)
            if not bin_max_counts.empty:
                bin_max_counts.to_excel(xw, sheet_name='size_bins_max', index=False)

    filename = f"{task_name or task_id}_helper.xlsx"
    return bio.getvalue(), filename


# 在文件内新增：按helper规范导出xlsx的工具函数
def _export_helper_xlsx_bak(request, results, task_id: str, task_name: str = ""):
    """将 OCRResult 列表导出为 helper 风格的 xlsx（多sheet）。

    返回: (bytes_io_value, filename)
    """
    import os
    import json as _json
    import numpy as _np
    import pandas as _pd
    from io import BytesIO
    from os.path import basename as _basename

    # 读取同任务汇总 JSON 以获取 confidences
    scores_map = {}
    try:
        report_dir = PathUtils.get_ocr_reports_dir()
        report_file = os.path.join(report_dir, f"{task_id}_ocr_summary.json")
        if os.path.exists(report_file):
            with open(report_file, 'r', encoding='utf-8') as jf:
                data = _json.load(jf)
                for item in data.get('items', []):
                    name = _basename(item.get('path', '') or '')
                    confs = item.get('confidences') or []
                    if isinstance(confs, list) and confs:
                        scores_map[name] = "|".join([
                            f"{float(c):.4f}" for c in confs if c is not None
                        ])
    except Exception:
        scores_map = {}

    # 读取首轮命中参数，填充参数列
    first_hit = {}
    try:
        report_dir = PathUtils.get_ocr_reports_dir()
        param_file = os.path.join(report_dir, f"{task_id}_first_hit.json")
        if os.path.exists(param_file):
            with open(param_file, 'r', encoding='utf-8') as fp:
                first_hit = _json.load(fp)
    except Exception:
        first_hit = {}

    # 分辨率缩放计算（与helper一致）
    def _compute_scaled(w: int, h: int, limit_type: str, side_len: int) -> str:
        try:
            w = int(w)
            h = int(h)
            side = int(side_len)
            if w <= 0 or h <= 0 or side <= 0:
                return f"{w}x{h}"
            cur_max = float(max(h, w))
            cur_min = float(min(h, w))
            lt = str(limit_type)
            if lt == 'max':
                if cur_max > side:
                    r = float(side) / cur_max
                    sw = int(round(w * r))
                    sh = int(round(h * r))
                    return f"{max(1, sw)}x{max(1, sh)}"
                return f"{w}x{h}"
            if lt == 'min':
                if cur_min < side:
                    r = float(side) / cur_min
                    sw = int(round(w * r))
                    sh = int(round(h * r))
                    return f"{max(1, sw)}x{max(1, sh)}"
                return f"{w}x{h}"
            # 兼容未知取值
            denom = float(min(h, w)) if lt == 'min' else float(max(h, w))
            if denom <= 0:
                return f"{w}x{h}"
            r = float(side) / denom
            sw = int(round(w * r))
            sh = int(round(h * r))
            return f"{max(1, sw)}x{max(1, sh)}"
        except Exception:
            return f"{w}x{h}"

    # 主表 data
    rows = []
    img_paths = []
    for r in results:
        img_path = PathUtils.normalize_path(r.image_path)
        img_paths.append(img_path)
        img_name = _basename(img_path)
        texts_list = r.texts if isinstance(r.texts, list) else []
        fh = first_hit.get(img_name) or {}
        # 分辨率
        base_w, base_h = (None, None)
        try:
            full = img_path if os.path.isabs(img_path) else os.path.join(settings.MEDIA_ROOT, img_path)
            data = _np.fromfile(full, dtype=_np.uint8)
            import cv2 as _cv2
            img_nd = _cv2.imdecode(data, _cv2.IMREAD_COLOR)
            if img_nd is not None:
                base_h, base_w = img_nd.shape[:2]
        except Exception:
            base_w, base_h = (None, None)
        resolution_str = f"{base_w}x{base_h}" if base_w and base_h else ''
        # scaled
        scaled_str = fh.get('scaled_resolution', '')
        if not scaled_str and base_w and base_h and fh.get('text_det_limit_type') and fh.get('text_det_limit_side_len'):
            scaled_str = _compute_scaled(base_w, base_h, str(fh.get('text_det_limit_type')),
                                         int(fh.get('text_det_limit_side_len')))
        if not scaled_str:
            scaled_str = resolution_str
        # texts/scores/num_items
        hit_texts = fh.get('hit_texts', '')
        texts_str = hit_texts
        scores_str = fh.get('hit_scores', '')
        num_items_val = len(str(hit_texts).split('|')) if hit_texts else 0
        rows.append({
            'image': img_name,
            'resolution': resolution_str,
            'scaled_resolution': scaled_str,
            'num_items': num_items_val,
            'scores': scores_str,
            'use_doc_orientation_classify': 'False',
            'use_doc_unwarping': str(bool(fh.get('use_doc_unwarping', False))),
            'use_textline_orientation': str(bool(fh.get('use_textline_orientation', False))),
            'text_det_limit_type': fh.get('text_det_limit_type', ''),
            'text_det_limit_side_len': fh.get('text_det_limit_side_len', ''),
            'text_det_thresh': fh.get('text_det_thresh', ''),
            'text_det_box_thresh': fh.get('text_det_box_thresh', ''),
            'text_det_unclip_ratio': fh.get('text_det_unclip_ratio', ''),
            'text_rec_score_thresh': fh.get('text_rec_score_thresh', ''),
            'texts': texts_str,
            'hit_round': fh.get('round', ''),
        })

    df_data = _pd.DataFrame(rows)

    # 尺寸与统计
    sizes = []
    for p in img_paths:
        try:
            full = p if os.path.isabs(p) else os.path.join(settings.MEDIA_ROOT, p)
            data = _np.fromfile(full, dtype=_np.uint8)
            import cv2 as _cv2
            img = _cv2.imdecode(data, _cv2.IMREAD_COLOR)
            if img is not None:
                h, w = img.shape[:2]
                sizes.append({'image': _basename(p), 'width': int(w), 'height': int(h),
                              'min_side': int(min(w, h)), 'max_side': int(max(w, h)),
                              'area': int(w) * int(h)})
        except Exception:
            continue
    df_sizes = _pd.DataFrame(sizes)

    desc = _pd.DataFrame()
    pcts = _pd.Series(dtype=float)
    bin_min_counts = _pd.DataFrame()
    bin_max_counts = _pd.DataFrame()
    summary_rows = []
    if not df_sizes.empty:
        try:
            desc = df_sizes[['min_side', 'max_side', 'width', 'height', 'area']].describe()
            pcts = df_sizes['min_side'].quantile([0.5, 0.75, 0.9, 0.95, 0.99]).rename('quantile')
            if not df_sizes.empty:
                summary_rows = [
                    {'metric': 'total_images', 'value': int(df_sizes.shape[0])},
                    {'metric': 'unique_images', 'value': int(df_sizes['image'].nunique())},
                    {'metric': 'p50_min_side', 'value': float(pcts.loc[0.5]) if 0.5 in pcts.index else None},
                    {'metric': 'p75_min_side', 'value': float(pcts.loc[0.75]) if 0.75 in pcts.index else None},
                    {'metric': 'p90_min_side', 'value': float(pcts.loc[0.9]) if 0.9 in pcts.index else None},
                    {'metric': 'p95_min_side', 'value': float(pcts.loc[0.95]) if 0.95 in pcts.index else None},
                    {'metric': 'p99_min_side', 'value': float(pcts.loc[0.99]) if 0.99 in pcts.index else None},
                ]
            # 直方分布
            bins_min = [0, 80, 100, 120, 140, 160, 180, 200, 240, 320, 480, 720, 960, 1280, 1600, _np.inf]
            labels_min = ['0-80', '80-100', '100-120', '120-140', '140-160', '160-180', '180-200', '200-240', '240-320',
                          '320-480', '480-720', '720-960', '960-1280', '1280-1600', '1600+']
            df_sizes['min_side_bin'] = _pd.cut(df_sizes['min_side'], bins=bins_min, labels=labels_min, right=False)
            bin_min_counts = df_sizes['min_side_bin'].value_counts().sort_index().rename('count').reset_index().rename(
                columns={'index': 'min_side_bin'})
            bins_max = [0, 160, 200, 240, 320, 480, 640, 720, 960, 1280, 1600, 1920, 2560, 3000, 4000, _np.inf]
            labels_max = ['0-160', '160-200', '200-240', '240-320', '320-480', '480-640', '640-720', '720-960',
                          '960-1280', '1280-1600', '1600-1920', '1920-2560', '2560-3000', '3000-4000', '4000+']
            df_sizes['max_side_bin'] = _pd.cut(df_sizes['max_side'], bins=bins_max, labels=labels_max, right=False)
            bin_max_counts = df_sizes['max_side_bin'].value_counts().sort_index().rename('count').reset_index().rename(
                columns={'index': 'max_side_bin'})
        except Exception:
            pass

    # 输出xlsx到内存
    bio = BytesIO()
    try:
        with _pd.ExcelWriter(bio, engine='openpyxl') as xw:
            df_data.to_excel(xw, sheet_name='data', index=False)
            if summary_rows:
                _pd.DataFrame(summary_rows).to_excel(xw, sheet_name='summary', index=False)
            if not desc.empty:
                desc.reset_index().rename(columns={'index': 'stat'}).to_excel(xw, sheet_name='size_stats', index=False)
            if not bin_min_counts.empty:
                bin_min_counts.to_excel(xw, sheet_name='size_bins_min', index=False)
            if not bin_max_counts.empty:
                bin_max_counts.to_excel(xw, sheet_name='size_bins_max', index=False)
    except Exception:
        with _pd.ExcelWriter(bio, engine='xlsxwriter') as xw:
            df_data.to_excel(xw, sheet_name='data', index=False)
            if summary_rows:
                _pd.DataFrame(summary_rows).to_excel(xw, sheet_name='summary', index=False)
            if not desc.empty:
                desc.reset_index().rename(columns={'index': 'stat'}).to_excel(xw, sheet_name='size_stats', index=False)
            if not bin_min_counts.empty:
                bin_min_counts.to_excel(xw, sheet_name='size_bins_min', index=False)
            if not bin_max_counts.empty:
                bin_max_counts.to_excel(xw, sheet_name='size_bins_max', index=False)

    filename = f"{task_name or task_id}_helper.xlsx"
    return bio.getvalue(), filename


# 在文件内新增：生成并上传 helper xlsx 的函数
def export_and_upload_helper_xlsx(task_id: str, bucket_name: str = "wfgame-ai"):
    """
    生成 helper xlsx 文件并上传到 MinIO，返回下载链接。
    """
    import os
    import tempfile
    from utils.minio_helper import upload_file

    task = OCRTask.objects.all_teams().filter(id=task_id).first()
    if not task:
        logger.error(f"OCR任务不存在，无法导出: {task_id}")
        return None

    results = task.related_results
    if not results:
        logger.error(f"OCR任务无结果，无法导出: {task_id}")
        return None

    try:
        # 1. 生成文件内容
        file_content, filename = _export_helper_xlsx(None, results, task_id)

        # 2. 保存到临时文件
        temp_dir = tempfile.gettempdir()
        temp_path = os.path.join(temp_dir, filename)

        with open(temp_path, 'wb') as f:
            f.write(file_content)

        # 3. 上传到 MinIO
        # 构造对象名称，例如: reports/ocr_helper/{task_id}/{filename}
        object_name = f"ocr_tasks/{task_id}/exports/{filename}"

        # 上传文件
        url = upload_file(bucket_name, object_name, temp_path,
                          content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

        return url

    except Exception as e:
        logger.error(f"导出并上传失败: {e}")
        return None
    finally:
        # 4. 清除临时文件
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception as e:
                logger.warning(f"删除临时文件失败: {e}")

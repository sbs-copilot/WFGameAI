"""
关键字过滤服务
用于检测OCR结果中是否包含指定的关键字
"""
import re
import logging
from typing import List, Dict, Tuple, Any

logger = logging.getLogger(__name__)


# OCR常见字符相似度映射（用于模糊匹配）
CHAR_SIMILARITY_MAP = {
    # 字母相似
    'K': ['X', 'C'],
    'X': ['K'],
    'O': ['0', 'D', 'Q'],
    'I': ['1', 'l', 'L'],
    'S': ['5'],
    'B': ['8'],
    'G': ['6'],
    'Z': ['2'],
    'M': ['N'],
    'N': ['M'],
    'C': ['G'],
    'E': ['F'],
    'Y': ['V'],
    # 数字相似
    '0': ['O'],
    '1': ['I', 'l'],
    '5': ['S'],
    '6': ['G'],
    '8': ['B'],
    '2': ['Z'],
}


def levenshtein_distance(s1: str, s2: str) -> float:
    """
    计算两个字符串的编辑距离（Levenshtein Distance）
    考虑OCR常见误识别，给相似字符更低的替换成本
    
    参数:
        s1: 字符串1
        s2: 字符串2
    
    返回:
        编辑距离
    """
    # 确保s1是较长的字符串（避免递归调用）
    if len(s1) < len(s2):
        s1, s2 = s2, s1
    
    if len(s2) == 0:
        return float(len(s1))
    
    # 初始化第一行
    previous_row = list(range(len(s2) + 1))
    
    for i, c1 in enumerate(s1):
        current_row = [i + 1]
        for j, c2 in enumerate(s2):
            # 计算插入、删除、替换的成本
            insertions = previous_row[j + 1] + 1
            deletions = current_row[j] + 1
            
            # 替换成本：如果字符相同为0，如果相似为0.5，否则为1
            if c1 == c2:
                substitution_cost = 0
            elif c1.upper() in CHAR_SIMILARITY_MAP.get(c2.upper(), []) or \
                 c2.upper() in CHAR_SIMILARITY_MAP.get(c1.upper(), []):
                substitution_cost = 0.5  # 相似字符的替换成本较低
            else:
                substitution_cost = 1
            
            substitutions = previous_row[j] + substitution_cost
            current_row.append(min(insertions, deletions, substitutions))
        
        previous_row = current_row
    
    return float(previous_row[-1])


def text_similarity(text1: str, text2: str, ignore_case: bool = True, 
                   ignore_spaces: bool = True, ignore_digits: bool = False) -> float:
    """
    计算两个文本的相似度 (0.0-1.0)
    
    参数:
        text1, text2: 待比较的文本
        ignore_case: 是否忽略大小写
        ignore_spaces: 是否忽略空格
        ignore_digits: 是否忽略数字和特殊字符（只保留字母）
    
    返回:
        相似度分数，1.0表示完全相同，0.0表示完全不同
    """
    # 预处理
    if ignore_case:
        text1 = text1.lower()
        text2 = text2.lower()
    
    if ignore_spaces:
        text1 = re.sub(r'\s+', '', text1)
        text2 = re.sub(r'\s+', '', text2)
    
    if ignore_digits:
        # 只保留字母（移除数字、标点、符号等）
        text1 = re.sub(r'[^a-zA-Z]', '', text1)
        text2 = re.sub(r'[^a-zA-Z]', '', text2)
    
    # 如果完全相同
    if text1 == text2:
        return 1.0
    
    # 如果处理后有任一文本为空
    if not text1 or not text2:
        return 0.0
    
    # 计算编辑距离
    distance = levenshtein_distance(text1, text2)
    max_len = max(len(text1), len(text2))
    
    if max_len == 0:
        return 0.0
    
    # 相似度 = 1 - (编辑距离 / 最大长度)
    similarity = 1.0 - (distance / max_len)
    
    return similarity


def fuzzy_match_text(text: str, target_phrases: List[str], min_similarity: float = 0.80,
                    ignore_case: bool = True, ignore_spaces: bool = True, 
                    ignore_digits: bool = False) -> Tuple[bool, float, str, str]:
    """
    模糊匹配文本是否与任一目标词组相似
    
    参数:
        text: 待匹配的文本
        target_phrases: 目标词组列表
        min_similarity: 最小相似度阈值
        ignore_case: 是否忽略大小写
        ignore_spaces: 是否忽略空格
        ignore_digits: 是否忽略数字和特殊字符
    
    返回:
        (是否匹配, 最高相似度, 匹配的文本, 匹配的目标词组)
    """
    max_similarity = 0.0
    matched_phrase = None
    
    # 遍历所有目标词组，找到相似度最高的
    for target_phrase in target_phrases:
        similarity = text_similarity(
            text, 
            target_phrase, 
            ignore_case, 
            ignore_spaces, 
            ignore_digits
        )
        if similarity > max_similarity:
            max_similarity = similarity
            matched_phrase = target_phrase
    
    if max_similarity >= min_similarity:
        return (True, max_similarity, text, matched_phrase)
    
    return (False, max_similarity, None, None)


class KeywordFilter:
    """关键字过滤器"""
    
    def __init__(self, config: Dict[str, Any]):
        """
        初始化关键字过滤器
        
        参数:
            config: 过滤配置
                - enabled: 是否启用
                - keywords: 关键字列表（逗号分隔的字符串）
                - fuzzy_match: 是否启用模糊匹配
                - fuzzy_similarity: 模糊匹配相似度阈值
                - ignore_case: 是否忽略大小写
                - ignore_spaces: 是否忽略空格
                - ignore_digits: 是否忽略数字和符号
                - min_confidence: OCR置信度阈值
        """
        self.enabled = config.get('enabled', False)
        self.keywords = []
        
        if self.enabled:
            keywords_str = config.get('keywords', '')
            self.keywords = [k.strip() for k in keywords_str.split(',') if k.strip()]
            
            self.fuzzy_match = config.get('fuzzy_match', True)
            self.fuzzy_similarity = config.get('fuzzy_similarity', 0.80)
            self.ignore_case = config.get('ignore_case', True)
            self.ignore_spaces = config.get('ignore_spaces', True)
            self.ignore_digits = config.get('ignore_digits', True)
            self.min_confidence = config.get('min_confidence', 0.80)
    
    def filter_results(self, ocr_results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        过滤OCR结果，根据关键字更新has_match字段
        
        参数:
            ocr_results: OCR结果列表，每个结果包含：
                - image_path: 图片路径
                - texts: 识别的文本列表
                - confidences: 置信度列表
                - has_match: 是否命中
                - ...
        
        返回:
            更新后的OCR结果列表
        """
        if not self.enabled or not self.keywords:
            return ocr_results
        
        total_count = len(ocr_results)
        language_matched = 0  # 语言匹配数量
        keyword_matched = 0   # 关键字匹配数量
        
        for result in ocr_results:
            # 第一步：检查是否通过语言检查（has_match由two_stage_ocr根据语言判断）
            if not result.get('has_match', False):
                # 未通过语言检查，直接跳过
                result['has_match'] = False
                continue
            
            language_matched += 1
            
            # 第二步：检查是否包含关键字
            if self._contains_keywords(result):
                # 通过语言检查 + 关键字检查 = 真正命中
                result['has_match'] = True
                keyword_matched += 1
            else:
                # 通过语言检查但未包含关键字
                result['has_match'] = False
        
        logger.info(f"关键字过滤完成: 原始={total_count}, 语言匹配={language_matched}, "
                   f"关键字匹配={keyword_matched}")
        
        return ocr_results
    
    def _contains_keywords(self, result: Dict[str, Any]) -> bool:
        """
        检查单个OCR结果是否包含关键字
        
        参数:
            result: OCR结果
        
        返回:
            是否包含关键字
        """
        texts = result.get('texts', [])
        # 兼容两种字段名：confidences（新）和scores（旧）
        confidences = result.get('confidences') or result.get('scores', [])
        
        if not texts:
            return False
        
        # 合并所有文本（用空格连接）
        combined_text = ' '.join(texts)
        
        # 如果启用了置信度过滤，只保留高置信度的文本
        if confidences and self.min_confidence > 0:
            high_conf_texts = [
                text for text, score in zip(texts, confidences) 
                if score >= self.min_confidence
            ]
            if not high_conf_texts:
                # 所有文本都被置信度过滤掉了
                logger.debug(f"⚠️  置信度过滤: {result.get('image_path')} - 所有文本置信度 < {self.min_confidence}")
                return False
            texts_to_check = high_conf_texts
        else:
            texts_to_check = texts
        
        # 检查是否包含任一关键字
        # 策略：先精确匹配（子串包含），再模糊匹配，最后检查组合文本
        if self.fuzzy_match:
            # 1. 先检查每个单独的文本
            for text in texts_to_check:
                # 1.1 先尝试精确匹配（检查是否包含关键字作为子串）
                for keyword in self.keywords:
                    pattern = self._build_pattern(keyword)
                    if pattern.search(text):
                        logger.debug(f"✅ 精确匹配(单文本): {result.get('image_path')} -> 关键字[{keyword}] 文本=[{text}]")
                        return True
                
                # 1.2 如果精确匹配失败，再尝试模糊匹配
                is_match, similarity, _, matched_phrase = fuzzy_match_text(
                    text,
                    self.keywords,
                    self.fuzzy_similarity,
                    self.ignore_case,
                    self.ignore_spaces,
                    self.ignore_digits
                )
                if is_match:
                    logger.debug(f"✅ 模糊匹配(单文本): {result.get('image_path')} -> 关键字[{matched_phrase}] 相似度={similarity:.2%}")
                    return True
            
            # 2. 如果单个文本都不匹配，再检查组合文本
            combined_text = ' '.join(texts_to_check)
            
            # 2.1 先尝试精确匹配组合文本
            for keyword in self.keywords:
                pattern = self._build_pattern(keyword)
                if pattern.search(combined_text):
                    logger.debug(f"✅ 精确匹配(组合): {result.get('image_path')} -> 关键字[{keyword}]")
                    return True
            
            # 2.2 再尝试模糊匹配组合文本
            is_match, similarity, _, matched_phrase = fuzzy_match_text(
                combined_text,
                self.keywords,
                self.fuzzy_similarity,
                self.ignore_case,
                self.ignore_spaces,
                self.ignore_digits
            )
            if is_match:
                logger.debug(f"✅ 模糊匹配(组合): {result.get('image_path')} -> 关键字[{matched_phrase}] 相似度={similarity:.2%}")
                return True
        else:
            # 精确匹配
            for keyword in self.keywords:
                pattern = self._build_pattern(keyword)
                if pattern.search(combined_text):
                    logger.debug(f"✅ 精确匹配: {result.get('image_path')} -> 关键字[{keyword}]")
                    return True
        
        return False
    
    def _build_pattern(self, keyword: str) -> re.Pattern:
        """
        构建正则表达式模式
        
        参数:
            keyword: 关键字
        
        返回:
            编译后的正则表达式
        """
        if self.ignore_spaces:
            compact = re.sub(r"\s+", "", keyword)
            pattern_str = r"\s*".join(map(re.escape, compact))
        else:
            pattern_str = r"\s+".join(map(re.escape, keyword.split()))
        
        flags = re.I if self.ignore_case else 0
        return re.compile(pattern_str, flags)

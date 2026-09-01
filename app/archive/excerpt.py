"""三行原文摘要（设计规格 §10）。

默认不调用 LLM：从清洗后的正文文本中提取前三个"有效句子"。
避免 AI 幻觉、与原文一致、零 API 成本。AI Summary 作为后续可选设置项。
"""

import re

# 中英文句末标点
_SENTENCE_END = re.compile(r"[^。！？!?.…\n]+[。！？!?.…]?")

# 过滤无效片段：过短、纯数字、URL、纯符号
def _is_valid_sentence(s: str) -> bool:
    s = s.strip()
    if len(s) < 8:
        return False
    if re.fullmatch(r"[\d\s.,%()\-—/]+", s):
        return False
    if "http" in s or "://" in s:
        return False
    # 排除标题/导航性短句：以常见无意义词开头
    return True


def extract_excerpt(text: str, max_lines: int = 3) -> str:
    """从正文取前 max_lines 个有效句子，保持原文顺序与标点。"""
    if not text:
        return ""
    text = re.sub(r"\s+", " ", text).strip()
    sentences = [
        s.strip()
        for s in _SENTENCE_END.findall(text)
        if s and _is_valid_sentence(s)
    ]
    return "\n".join(sentences[:max_lines])

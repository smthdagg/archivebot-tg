"""失败自动重试分类（M7 遗留）。

可重试错误 = 瞬态失败（网络/超时/图片或 PDF 生成中的瞬时故障/Telegram 上传），
重试大概率可恢复；不可重试错误 = 确定性的永久失败（SSRF INVALID_URL、权限
HTTP_FORBIDDEN、登录 LOGIN_REQUIRED、404 NOT_FOUND、空内容 EMPTY_CONTENT、
存储满 STORAGE_FULL），重试无意义，直接 FAILED。
"""

from app.database.enums import ErrorCode

# 瞬态失败：异常重试可恢复
RETRYABLE_ERROR_CODES: frozenset[str] = frozenset({
    ErrorCode.TIMEOUT,
    ErrorCode.IMAGE_DOWNLOAD_FAILED,
    ErrorCode.PDF_GENERATION_FAILED,
    ErrorCode.TELEGRAM_UPLOAD_FAILED,
    # 未知错误大多是瞬态网络异常（Playwright / curl_cffi 未归类），重试兜底；
    # 真 bug 会在耗尽 retry_count 后照常 FAILED，不改变语义。
    ErrorCode.UNKNOWN,
})


def is_retryable(code: str) -> bool:
    """返回该错误码是否应自动重试。"""
    return code in RETRYABLE_ERROR_CODES

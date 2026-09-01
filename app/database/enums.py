"""枚举与常量（对应设计规格 §8/§21/§39）。"""

from enum import StrEnum


class UserRole(StrEnum):
    USER = "USER"
    ADMIN = "ADMIN"
    SUPER_ADMIN = "SUPER_ADMIN"


class UserStatus(StrEnum):
    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"
    DELETED = "DELETED"


class ApplicationStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class TaskStatus(StrEnum):
    QUEUED = "QUEUED"
    FETCHING = "FETCHING"
    PARSING = "PARSING"
    DOWNLOADING_IMAGES = "DOWNLOADING_IMAGES"
    GENERATING_MARKDOWN = "GENERATING_MARKDOWN"
    GENERATING_PDF = "GENERATING_PDF"
    UPLOADING = "UPLOADING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

    @classmethod
    def processing_statuses(cls) -> frozenset[str]:
        """运行中的状态（禁止清理其临时文件，规格 §32）。"""
        return frozenset({
            cls.FETCHING.value,
            cls.PARSING.value,
            cls.DOWNLOADING_IMAGES.value,
            cls.GENERATING_MARKDOWN.value,
            cls.GENERATING_PDF.value,
            cls.UPLOADING.value,
        })

    @classmethod
    def is_processing(cls, value: str) -> bool:
        return value in cls.processing_statuses()


class OutputType(StrEnum):
    PDF = "PDF"
    MARKDOWN = "MARKDOWN"
    IMAGES = "IMAGES"


class FileType(StrEnum):
    PDF = "PDF"
    MARKDOWN = "MARKDOWN"
    IMAGES_ZIP = "IMAGES_ZIP"
    COVER = "COVER"


# ---- 审计动作常量（设计规格 §39）----

class AuditAction(StrEnum):
    # User Log
    USER_REGISTERED = "USER_REGISTERED"
    USER_APPLY = "USER_APPLY"
    USER_APPROVED = "USER_APPROVED"
    USER_REJECTED = "USER_REJECTED"
    USER_DISABLED = "USER_DISABLED"
    USER_ENABLED = "USER_ENABLED"
    USER_DELETED = "USER_DELETED"
    # Task Log
    TASK_CREATED = "TASK_CREATED"
    TASK_STARTED = "TASK_STARTED"
    TASK_FETCHING = "TASK_FETCHING"
    TASK_PARSING = "TASK_PARSING"
    TASK_DOWNLOADING = "TASK_DOWNLOADING"
    TASK_PDF = "TASK_PDF"
    TASK_MARKDOWN = "TASK_MARKDOWN"
    TASK_UPLOADING = "TASK_UPLOADING"
    TASK_COMPLETED = "TASK_COMPLETED"
    TASK_FAILED = "TASK_FAILED"
    TASK_CANCELLED = "TASK_CANCELLED"
    # File Log
    FILE_CREATED = "FILE_CREATED"
    FILE_SENT = "FILE_SENT"
    FILE_DELETED = "FILE_DELETED"
    FILE_CLEANUP = "FILE_CLEANUP"
    # Admin Log
    ADMIN_LOGIN = "ADMIN_LOGIN"
    ADMIN_LOCKOUT = "ADMIN_LOCKOUT"
    USER_APPROVE = "USER_APPROVE"
    USER_DISABLE = "USER_DISABLE"
    USER_DELETE = "USER_DELETE"
    TASK_RETRY = "TASK_RETRY"
    TASK_CANCEL = "TASK_CANCEL"
    SETTINGS_CHANGED = "SETTINGS_CHANGED"


# ---- 平台识别常量 ----

class Platform(StrEnum):
    WECHAT = "wechat"
    TWITTER = "twitter"
    XHS = "xhs"
    WEIBO = "weibo"
    ZHIHU = "zhihu"
    REDDIT = "reddit"
    YOUTUBE = "youtube"
    BILIBILI = "bilibili"
    DOUYIN = "douyin"
    KUAISHOU = "kuaishou"
    INSTAGRAM = "instagram"
    THREADS = "threads"
    PINTEREST = "pinterest"
    FEISHU = "feishu"
    WEB = "web"
    UNKNOWN = "unknown"


# ---- 错误码（任务失败分类，设计规格 §36）----

class ErrorCode(StrEnum):
    INVALID_URL = "INVALID_URL"
    NOT_FOUND = "NOT_FOUND"
    HTTP_FORBIDDEN = "HTTP_FORBIDDEN"
    LOGIN_REQUIRED = "LOGIN_REQUIRED"
    TIMEOUT = "TIMEOUT"
    EMPTY_CONTENT = "EMPTY_CONTENT"
    IMAGE_DOWNLOAD_FAILED = "IMAGE_DOWNLOAD_FAILED"
    PDF_GENERATION_FAILED = "PDF_GENERATION_FAILED"
    TELEGRAM_UPLOAD_FAILED = "TELEGRAM_UPLOAD_FAILED"
    STORAGE_FULL = "STORAGE_FULL"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"

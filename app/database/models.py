"""数据库模型（对应设计规格 §48）。"""

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    language: Mapped[str] = mapped_column(String(8), default="auto")
    role: Mapped[str] = mapped_column(String(16), default="USER")
    status: Mapped[str] = mapped_column(String(16), default="PENDING", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_active_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    tasks: Mapped[list["Task"]] = relationship(back_populates="user")


class UserApplication(Base):
    __tablename__ = "user_applications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="PENDING", index=True)
    reviewed_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    url: Mapped[str] = mapped_column(Text)
    platform: Mapped[str | None] = mapped_column(String(32), nullable=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    author: Mapped[str | None] = mapped_column(String(128), nullable=True)
    published_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)  # 三行原文摘要
    status: Mapped[str] = mapped_column(String(24), default="QUEUED", index=True)
    output_types: Mapped[list] = mapped_column(JSON, default=list)
    # 失败自动重试已消耗次数（M7）。0=初始；耗尽配置 retry_count 后 FAILED。
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 任务目录名（storage/tasks/<task_uuid>）
    storage_uuid: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # 处理中状态消息的 message_id（worker 编辑进度用）
    status_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(default=False)
    # 任务指定的 Cookie Profile 名（登录类网站，Phase 2；nullable=未指定）
    cookie_profile: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped["User"] = relationship(back_populates="tasks")
    files: Mapped[list["File"]] = relationship(back_populates="task")


class File(Base):
    __tablename__ = "files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"), index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    type: Mapped[str] = mapped_column(String(16))  # PDF / MARKDOWN / IMAGES_ZIP / COVER / VIDEO
    filename: Mapped[str] = mapped_column(String(255))
    size: Mapped[int] = mapped_column(Integer, default=0)
    local_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    telegram_file_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    uploaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    task: Mapped["Task"] = relationship(back_populates="files")


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    operator_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(32), index=True)
    target_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    target_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    details: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SystemSetting(Base):
    __tablename__ = "system_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


# ---------------------------------------------------------------------------
# 公众号订阅跟踪（微信读书路线，Phase 2）
# ---------------------------------------------------------------------------

class WxMpAccount(Base):
    """公众号账号级状态：服务端微信读书账号侧的订阅与增量游标。

    account_id 是微信读书书架上的 bookId（格式 ``MP_WXS_<数字>``）。
    last_synckey 是微信读书的增量刷新游标（非页码），每次拉取后原样回存。
    """

    __tablename__ = "wx_mp_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    mp_name: Mapped[str] = mapped_column(String(128), default="")
    last_synckey: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    active: Mapped[bool] = mapped_column(default=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class WxSubscription(Base):
    """用户对某公众号的订阅（每用户每号一条）。

    last_article_time 是该订阅者的分发基线（Unix 秒）：订阅时刻取当前时间，
    因此不分发历史文章，只分发基线之后的新文章（防首拉洪水）。
    """

    __tablename__ = "wx_subscriptions"
    __table_args__ = (
        UniqueConstraint("user_id", "account_id", name="uq_wx_sub_user_account"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    account_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("wx_mp_accounts.account_id"), index=True
    )
    delivery_mode: Mapped[str] = mapped_column(String(16), default="notify")
    # auto 模式的默认输出格式（[OutputType.value]）；notify 模式忽略
    output_types: Mapped[list] = mapped_column(JSON, default=list)
    last_article_time: Mapped[int] = mapped_column(BigInteger, default=0)
    active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class WxPendingArticle(Base):
    """notify 模式推送出去的文章登记（一键归档按钮的 callback 间接层）。

    公众号文章 URL 远超 Telegram callback_data 的 64 字节上限，按钮只带
    本表主键（``wsubgo:{id}``），点击后回查 doc_url 建任务。定期清理过期行。
    """

    __tablename__ = "wx_pending_articles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[str] = mapped_column(String(64), index=True)
    doc_url: Mapped[str] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    article_time: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

"""pytest 全局配置：在任何 app 模块导入前设置测试环境变量。"""

import os
import tempfile

os.environ["STORAGE_DIR"] = tempfile.mkdtemp(prefix="archivebot-test-storage-")
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["TELEGRAM_BOT_TOKEN"] = "test-token"
os.environ["REDIS_URL"] = "redis://localhost:6379/15"

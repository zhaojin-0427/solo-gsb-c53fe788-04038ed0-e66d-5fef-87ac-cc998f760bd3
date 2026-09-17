"""应用配置：全部可通过环境变量覆盖。"""
import os
from pathlib import Path

APP_TITLE = "样本链路与隔离处置台"

# SQLite 数据库文件路径（容器内默认 /data/app.db，通过卷持久化）
DB_PATH = os.environ.get("DB_PATH", str(Path(__file__).resolve().parents[2] / "data" / "app.db"))

# 会话有效期（小时）
SESSION_TTL_HOURS = int(os.environ.get("SESSION_TTL_HOURS", "12"))

# 首次启动时是否写入演示数据（一批次 + 若干样本 + 一次交接 + 一次超限）
SEED_DEMO_DATA = os.environ.get("SEED_DEMO_DATA", "false").strip().lower() in ("1", "true", "yes", "on")

# 前端静态文件目录：优先环境变量，其次仓库内 frontend/（本地开发），最后 /app/static（容器）
_candidates = [
    os.environ.get("STATIC_DIR", ""),
    str(Path(__file__).resolve().parents[2] / "frontend"),
    "/app/static",
]
STATIC_DIR = next((p for p in _candidates if p and Path(p).is_dir()), _candidates[-1])

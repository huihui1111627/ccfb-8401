import os
from pathlib import Path


class Config:
    """运行期配置，全部可用环境变量覆盖，方便测试隔离。"""

    def __init__(self, home: str | None = None):
        self.home = Path(home or os.environ.get("CTPLATFORM_HOME", "data")).resolve()
        self.db_path = Path(
            os.environ.get("CTPLATFORM_DB", str(self.home / "ctplatform.duckdb"))
        )
        self.upload_dir = self.home / "uploads"
        self.sample_dir = self.home / "samples"

    def ensure_dirs(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        self.sample_dir.mkdir(parents=True, exist_ok=True)


CONFIG = Config()

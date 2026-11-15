import os
from dataclasses import dataclass
from pathlib import Path
from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    root: Path
    token: str = ""
    upload_bytes: int = 20_000_000
    user_bytes: int = 256 * 1024 * 1024
    total_bytes: int = 2 * 1024 * 1024 * 1024
    max_jobs: int = 50
    job_seconds: int = 600

    @classmethod
    def env(cls):
        load_dotenv()
        return cls(
            root=Path(os.getenv("DATA_DIR", "data")).resolve(),
            token=os.getenv("BOT_TOKEN", ""),
            user_bytes=int(os.getenv("USER_STORAGE_MB", "256")) * 1024**2,
            total_bytes=int(os.getenv("TOTAL_STORAGE_MB", "2048")) * 1024**2,
            job_seconds=int(os.getenv("JOB_TIMEOUT_SECONDS", "600")),
        )

    def user_dir(self, user: int) -> Path:
        if user <= 0:
            raise ValueError("Invalid user")
        path = self.root / "users" / str(user)
        path.mkdir(parents=True, exist_ok=True)
        return path

from pydantic_settings import BaseSettings
from pathlib import Path

class Settings(BaseSettings):
    # AI
    anthropic_api_key: str

    # 目标系统
    base_url: str = ""
    sys_username: str = ""
    sys_password: str = ""

    # 数据库
    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = ""
    db_user: str = ""
    db_pass: str = ""

    # 外部 API
    ext_api_base: str = ""
    ext_api_token: str = ""

    # 执行配置
    headless: bool = False
    slow_mo: int = 50
    screenshot_on_fail: bool = True
    output_dir: Path = Path("./scripts")
    frames_dir: Path = Path("./frames")

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"

settings = Settings()
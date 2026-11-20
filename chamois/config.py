from enum import StrEnum

from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict


class Role(StrEnum):
    RESEARCHER = "researcher"  # submits circuits, sees own jobs
    OPERATOR = "operator"  # sees the whole queue, edits calibration, cancels any job


class User(BaseModel):
    name: str
    role: Role


# Public demo tokens: they are printed in the README on purpose.
DEMO_TOKENS = {
    "researcher-demo-token": User(name="Researcher (demo)", role=Role.RESEARCHER),
    "operator-demo-token": User(name="Operator (demo)", role=Role.OPERATOR),
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CHAMOIS_", env_file=".env", extra="ignore")

    workspace_id: str = "default"
    # 1.0 = the device takes as long as the real shots would; tests use 0.
    time_scale: float = 1.0
    max_queued_jobs: int = 50
    keep_jobs: int = 200
    # Production and CI set this, so a missing Rust build fails loudly.
    require_rust: bool = False
    # JSON in the environment, e.g. CHAMOIS_TOKENS='{"secret": {"name": "Ana", "role": "operator"}}'
    tokens: dict[str, User] = DEMO_TOKENS
    cors_origins: list[str] = []
    crosscheck_enabled: bool = True
    seed: int | None = None

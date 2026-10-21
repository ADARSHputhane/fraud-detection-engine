import socket
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration, read from environment variables or a .env file."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://fraud:fraud@localhost:5432/fraud"
    redis_url: str = "redis://localhost:6379/0"
    stream_name: str = "events:incoming"
    # Approximate cap on stream length so Redis memory stays bounded.
    stream_maxlen: int = 100_000
    # Largest batch accepted by POST /events/batch.
    max_batch_size: int = 1_000

    # Worker: consumer-group name shared by all workers, and this worker's own name.
    consumer_group: str = "scorers"
    consumer_name: str = Field(default_factory=socket.gethostname)
    worker_batch_size: int = 100
    worker_block_ms: int = 1_000
    # Events that fail processing are parked here instead of blocking the stream.
    deadletter_stream: str = "events:deadletter"
    # Prefix for the feature engine's Redis keys.
    feature_prefix: str = "fe"


@lru_cache
def get_settings() -> Settings:
    return Settings()

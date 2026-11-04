import os
import tomllib
from functools import cache
from pathlib import Path

from pydantic import BaseModel


class ConfigData(BaseModel):
    """Raw configuration data loaded from the TOML file."""

    cookies_file: str
    browser: str
    download_dir: str
    server_version: str
    max_request_size: int
    request_timeout: int
    ip: str
    port: int


class Config:
    """Configuration management class."""

    @staticmethod
    @cache
    def root() -> Path:
        """Return the root directory of the application."""
        if "PROJECT_ROOT" in os.environ:
            return Path(os.environ["PROJECT_ROOT"]).resolve()

        current = Path(__file__).resolve().parent
        for parent in (current, *current.parents):
            if (parent / "pyproject.toml").is_file():
                return parent

        msg = "Could not determine project root"
        raise RuntimeError(msg)

    @staticmethod
    @cache
    def data() -> ConfigData:
        """Load and return the configuration data from the TOML file."""
        config_path = Config.root() / "config.toml"
        if not config_path.is_file():
            msg = f"Configuration file not found: {config_path}"
            raise FileNotFoundError(msg)

        with config_path.open("rb") as f:
            config_dict = tomllib.load(f)

        return ConfigData.model_validate(config_dict)

    @staticmethod
    @cache
    def download_dir() -> Path:
        """Return the download directory as a Path object."""
        return Path(Config.data().download_dir).resolve()


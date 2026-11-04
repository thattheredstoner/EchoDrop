import logging

from src.echodrop.server import serve


def setup_logging() -> None:
    """Set up logging configuration."""
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

if __name__ == "__main__":
    setup_logging()
    serve()

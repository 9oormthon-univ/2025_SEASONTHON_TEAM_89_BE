import logging

# enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO)

LOGGER = logging.getLogger(__name__)

from app.config import Development as Config

WEB_HOST = Config.WEB_HOST
WEB_PORT = Config.WEB_PORT

OLLAMA_URL = Config.OLLAMA_URL
OLLAMA_MODEL = Config.OLLAMA_MODEL
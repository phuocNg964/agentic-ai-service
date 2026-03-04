import logging
import sys

def setup_logging():
    """
    Configure logging for the application.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout)
        ]
    )
    
    # Set lower noise for some libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("weaviate").setLevel(logging.WARNING)

logger = logging.getLogger("promet_ai")

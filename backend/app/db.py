import sqlite3
import logging
import mysql.connector
from urllib.parse import urlparse
from .config import settings

logger = logging.getLogger(__name__)

def get_db_connection():
    """Establishes and returns a database connection based on the DATABASE_URL."""
    try:
        db_url = settings.DATABASE_URL
        parsed_url = urlparse(db_url)

        if parsed_url.scheme == "sqlite":
            db_path = settings.DATABASE_URL.replace("sqlite:///", "")
            conn = sqlite3.connect(db_path)
            logger.info(f"Successfully connected to SQLite database at: {db_path}")
            return conn
        elif parsed_url.scheme == "mysql":
            # Safely parse port — fall back to 3306 if missing or invalid
            try:
                port = parsed_url.port
            except ValueError:
                logger.warning(
                    f"DATABASE_URL contains invalid port (raw: {parsed_url.netloc}), "
                    f"falling back to default MySQL port 3306. "
                    f"Please fix DATABASE_URL format: mysql://user:pass@host:3306/dbname"
                )
                port = 3306
            if port is None:
                port = 3306

            conn = mysql.connector.connect(
                host=parsed_url.hostname,
                port=port,
                user=parsed_url.username,
                password=parsed_url.password,
                database=parsed_url.path.lstrip('/')
            )
            logger.info(f"Successfully connected to MySQL database at: {parsed_url.hostname}:{port}")
            return conn
        else:
            logger.error(f"Unsupported database scheme: {parsed_url.scheme}")
            return None
            
    except (sqlite3.Error, mysql.connector.Error) as e:
        logger.error(f"Database connection failed to '{settings.DATABASE_URL}': {e}", exc_info=True)
        return None
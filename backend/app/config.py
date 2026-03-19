from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    # AI Backend: "openai" or "echo"
    AI_BACKEND: str = "openai"

    # OpenAI API Settings
    OPENAI_API_KEY: str | None = None # Allow key to be optional to enable server startup
    OPENAI_BASE_URL: str = "https://api.openai.com/v1"
    OPENAI_MODEL: str = "gpt-3.5-turbo"
    OPENAI_MODEL_CHEAT_CHECK: str = "qwen3-235b-a22b"
    
    # Echo Agent API Settings (alternative to OpenAI)
    ECHO_API_URL: str = ""              # e.g. http://172.20.80.1:8006
    ECHO_API_KEY: str | None = None      # Authorization header value
    ECHO_AGENT_ID: str = ""              # Agent ID for game master
    ECHO_AGENT_ID_CHEAT: str = ""        # Agent ID for cheat check (optional, falls back to ECHO_AGENT_ID)
    ECHO_MONGO_ID: str = "public-db"     # x-mongo-id header

    # Image Generation Settings (optional)
    IMAGE_GEN_MODEL: str | None = None
    IMAGE_GEN_BASE_URL: str | None = None
    IMAGE_GEN_API_KEY: str | None = None
    IMAGE_GEN_IDLE_SECONDS: int = 10

    # JWT Settings for OAuth2
    SECRET_KEY: str
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 600

    # Database URL
    DATABASE_URL: str = "sqlite:///./veloera.db"

    # Linux.do OAuth Settings
    LINUXDO_CLIENT_ID: str | None = None
    LINUXDO_CLIENT_SECRET: str | None = None
    LINUXDO_SCOPE: str = "read"

    # Email Auth Settings
    SMTP_HOST: str = ""
    SMTP_PORT: int = 465
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_USE_SSL: bool = True
    SMTP_FROM_NAME: str = "浮生十梦"

    # Server Settings
    HOST: str = "127.0.0.1"
    PORT: int = 8000
    UVICORN_RELOAD: bool = True

    # Point to the .env file in the 'backend' directory relative to the project root
    model_config = SettingsConfigDict(env_file="backend/.env")

# Create a single instance of the settings
settings = Settings()
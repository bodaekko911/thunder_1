from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    APP_NAME: str = "ERP System"
    SECRET_KEY: str = "mysupersecretkey123456"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 480
    DATABASE_URL: str = "postgresql://postgres:yourpassword@localhost:5432/erp"

    class Config:
        env_file = ".env"
        extra = "ignore"

settings = Settings()
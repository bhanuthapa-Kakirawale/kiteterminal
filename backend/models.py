from pydantic import BaseModel


class SessionStatus(BaseModel):
    connected: bool
    user_name: str | None = None
    login_time: str | None = None


class ApiError(BaseModel):
    error_code: str
    message: str

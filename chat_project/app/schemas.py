from pydantic import BaseModel
from datetime import datetime

class UserCreate(BaseModel):
    username: str
    password: str
    is_admin: bool = False

class UserResponse(BaseModel):
    id: int
    username: str
    is_admin: bool
    class Config:
        from_attributes = True

class MessageResponse(BaseModel):
    id: int
    content: str
    timestamp: datetime
    owner: UserResponse
    class Config:
        from_attributes = True
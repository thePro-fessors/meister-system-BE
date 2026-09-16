# 표준응답규격(Standard response format)

from pydantic import BaseModel, Field
from typing import TypeVar, Generic

T = TypeVar('T')

class Error(BaseModel):
    code: str
    message: str

class SrFormat(BaseModel, Generic[T]):
    status_code: int = Field(200, description="HTTP Status Code")
    success: bool = Field(False, description='Success')
    data: T | None = Field(None, description='Response Data')
    error: Error | None = Field(None, description='Error Data')


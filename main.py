from fastapi import *
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

import sr_format
from sr_format import *

app = FastAPI()

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request, exc):
    first_error = exc.errors()[0] if exc.errors() else {}
    field = " -> ".join([str(loc) for loc in first_error.get("loc", [])])
    reason = first_error.get("msg", "입력값이 올바르지 않습니다.")

    detail = f"{field}: {reason}" if  field else reason

    return JSONResponse(
        status_code=400,
        content=SrFormat(status_code=400, success=False, error=Error(code="VALIDATION_ERROR", message=detail)).model_dump()
    )

@app.get("/")
async def root():
    return SrFormat(status_code=200, success=True, data={"What am I":"I am nothing"})

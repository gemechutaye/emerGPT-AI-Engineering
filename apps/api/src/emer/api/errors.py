from fastapi import HTTPException


def problem(status: int, code: str, message: str, retryable: bool = False):
    return HTTPException(status, detail={"code": code, "message": message, "retryable": retryable})

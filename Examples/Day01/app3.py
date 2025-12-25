import asyncio
import logging
import uuid
from typing import Any, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, status
from pydantic import BaseModel, ValidationError, Field

from llm import llm  # your LangChain (or other) LLM object

logger = logging.getLogger("ws-chat")

app = FastAPI(
    title="Hirect",
    description="FastAPI WebSocket",
    version="1.0.0",
    docs_url="/swagger",
    redoc_url="/redoc",
)


# ---------------------------
# 1) Request Schema
# ---------------------------
class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=8000)


# ---------------------------
# 2) Utilities
# ---------------------------
def to_text(result: Any) -> str:
    """Convert LLM result / chunk to plain text safely."""
    if result is None:
        return ""
    if hasattr(result, "content"):
        return result.content or ""
    return str(result)


async def safe_close(websocket: WebSocket, code: int) -> None:
    """Best-effort close with a WebSocket close code."""
    try:
        await websocket.close(code=code)
        logger.info("WebSocket closed with code=%s", code)
    
    except Exception:
        pass


def is_message_too_big_error(errors: list[dict]) -> bool:
    """
        Detect Pydantic 'max_length' errors so we can close with 1009 (Message Too Big).
        Pydantic error structures can vary slightly across versions.
    """
    for err in errors:
        # Typical fields: {'type': 'string_too_long', 'loc': ('message',), ...}
        err_type = (err.get("type") or "").lower()
        msg = (err.get("msg") or "").lower()
        if "too_long" in err_type or "too long" in msg or "max_length" in msg:
            # Only treat it as message-too-big if it's related to the 'message' field
            loc = err.get("loc", ())
            if isinstance(loc, (list, tuple)) and "message" in loc:
                return True
    return False


async def receive_chat_request(websocket: WebSocket) -> Optional[ChatRequest]:
    """
        Receive JSON and validate with Pydantic.
        - Returns ChatRequest if valid.
        - Returns None if invalid (also sends error message).
        - Raises WebSocketDisconnect if client disconnects.
    """
    try:
        data = await websocket.receive_json()
        return ChatRequest.model_validate(data)  # Pydantic v2
    
    except WebSocketDisconnect:
        raise
    
    except ValidationError as ve:
        errors = ve.errors()
        # Send a structured error response; keep connection open by default.
        await websocket.send_json({
            "type": "error",
            "code": "INVALID_PAYLOAD",
            "detail": errors,
            "example": {"message": "Hi"}
        })
        return None
    
    except Exception as e:
        await websocket.send_json({
            "type": "error",
            "code": "BAD_REQUEST",
            "detail": str(e),
            "example": {"message": "Hi"}
        })
        return None


# ---------------------------
# 3) Endpoint 01 (Non-stream)
# ---------------------------
@app.websocket("/ws/chat")
async def ws_chat_non_stream(websocket: WebSocket):
    await websocket.accept()
    conn_id = str(uuid.uuid4())[:8]
    logger.info("WS connected /ws/chat conn_id=%s", conn_id)

    close_code = status.WS_1000_NORMAL_CLOSURE
    lock = asyncio.Lock()

    try:
        while True:
            # Receive + validate
            try:
                data = await websocket.receive_json()
                req = ChatRequest.model_validate(data)
            
            except ValidationError as ve:
                errors = ve.errors()
                # If message too large -> close with 1009
                if is_message_too_big_error(errors):
                    await websocket.send_json({
                        "type": "error",
                        "code": "MESSAGE_TOO_BIG",
                        "detail": "Message exceeds max length."
                    })
                    close_code = status.WS_1009_MESSAGE_TOO_BIG
                    break

                # Otherwise: send error and continue
                await websocket.send_json({
                    "type": "error",
                    "code": "INVALID_PAYLOAD",
                    "detail": errors,
                    "example": {"message": "Hi"}
                })
                continue

            async with lock:
                request_id = str(uuid.uuid4())[:8]
                try:
                    result = await asyncio.wait_for(llm.ainvoke(req.message), timeout=60)
                    reply_text = to_text(result)

                    await websocket.send_json({
                        "type": "response",
                        "request_id": request_id,
                        "value": reply_text
                    })

                except asyncio.TimeoutError:
                    # Tell client; optionally close with 1013 (Try Again Later)
                    await websocket.send_json({
                        "type": "error",
                        "code": "LLM_TIMEOUT",
                        "request_id": request_id,
                        "detail": "LLM call exceeded 60 seconds."
                    })
                    # If you want to force client reconnect/backoff, uncomment:
                    close_code = status.WS_1013_TRY_AGAIN_LATER
                    break

                except Exception as e:
                    logger.exception("LLM error conn_id=%s request_id=%s", conn_id, request_id)
                    await websocket.send_json({
                        "type": "error",
                        "code": "LLM_ERROR",
                        "request_id": request_id,
                        "detail": str(e)
                    })
                    close_code = status.WS_1011_INTERNAL_ERROR
                    break

    except WebSocketDisconnect:
        logger.info("WS disconnected /ws/chat conn_id=%s", conn_id)
        # Client disconnected; no need to close.
        return

    except Exception:
        logger.exception("WS fatal error /ws/chat conn_id=%s", conn_id)
        close_code = status.WS_1011_INTERNAL_ERROR

    finally:
        await safe_close(websocket, code=close_code)


# ---------------------------
# 4) Endpoint 02 (Streaming)
# ---------------------------
@app.websocket("/ws/chat2")
async def ws_chat_stream(websocket: WebSocket):
    await websocket.accept()
    conn_id = str(uuid.uuid4())[:8]
    logger.info("WS connected /ws/chat2 conn_id=%s", conn_id)

    close_code = status.WS_1000_NORMAL_CLOSURE
    lock = asyncio.Lock()

    try:
        while True:
            # Receive + validate
            try:
                data = await websocket.receive_json()
                req = ChatRequest.model_validate(data)
            
            except ValidationError as ve:
                errors = ve.errors()
                if is_message_too_big_error(errors):
                    await websocket.send_json({
                        "type": "error",
                        "status": "MESSAGE_TOO_BIG",
                        "status_code": status.WS_1009_MESSAGE_TOO_BIG,
                        "detail": "Message exceeds max length."
                    })
                    close_code = status.WS_1009_MESSAGE_TOO_BIG
                    break

                await websocket.send_json({
                    "type": "error",
                    "status": "INVALID_PAYLOAD",
                    "status_code": status.HTTP_422_UNPROCESSABLE_ENTITY,
                    "detail": errors,
                    "example": {"message": "Hi"}
                })
                continue

            async with lock:
                request_id = str(uuid.uuid4())[:8]
                await websocket.send_json({"type": "start", "request_id": request_id})

                full_text = ""

                async def stream_tokens():
                    nonlocal full_text
                    async for chunk in llm.astream(req.message):
                        token = to_text(chunk)
                        if not token:
                            continue
                        full_text += token
                        await websocket.send_json({
                            "type": "chunk",
                            "request_id": request_id,
                            "value": token,
                            "status": "IN_PROGRESS",
                            "status_code": status.HTTP_200_OK
                        })

                try:
                    await asyncio.wait_for(stream_tokens(), timeout=120)

                    await websocket.send_json({
                        "type": "end",
                        "request_id": request_id,
                        "full": full_text,
                        "status": "COMPLETED",
                        "status_code": status.HTTP_200_OK
                    })

                except asyncio.TimeoutError:
                    await websocket.send_json({
                        "type": "error",
                        "code": "STREAM_TIMEOUT",
                        "request_id": request_id,
                        "detail": "Streaming exceeded 120 seconds."
                    })
                    # Force reconnect/backoff if desired:
                    close_code = status.WS_1013_TRY_AGAIN_LATER
                    break

                except WebSocketDisconnect:
                    raise

                except Exception as e:
                    logger.exception("Streaming error conn_id=%s request_id=%s", conn_id, request_id)
                    try:
                        await websocket.send_json({
                            "type": "error",
                            "code": "STREAM_ERROR",
                            "request_id": request_id,
                            "detail": str(e)
                        })
                    except Exception:
                        pass
                    
                    close_code = status.WS_1011_INTERNAL_ERROR
                    break

    except WebSocketDisconnect:
        logger.info("WS disconnected /ws/chat2 conn_id=%s", conn_id)
        return

    except Exception:
        logger.exception("WS fatal error /ws/chat2 conn_id=%s", conn_id)
        close_code = status.WS_1011_INTERNAL_ERROR

    finally:
        await safe_close(websocket, code=close_code)

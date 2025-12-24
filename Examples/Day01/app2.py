import asyncio
import logging
import uuid
from typing import Any, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, status
from pydantic import BaseModel, ValidationError, Field

from llm import llm  # your LangChain (or other) LLM object


# Define the Logger:
logger = logging.getLogger("ws-chat")


# Define the FastAPI APP:
app = FastAPI(
    title="Hirect",
    description="FastAPI WebSocket",
    version="1.0.0",
    docs_url="/swagger",
    redoc_url="/redoc",
)


# ---------------------------
# 1) Request/Response Schemas
# ---------------------------

class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=8000)


def to_text(result: Any) -> str:
    """Convert LLM result / chunk to plain text safely."""
    if result is None:
        return ""
    
    if hasattr(result, "content"):
        return result.content or ""
    
    return str(result)


async def safe_close(websocket: WebSocket, code: int = status.WS_1000_NORMAL_CLOSURE) -> None:
    """Best-effort close."""
    try:
        await websocket.close(code=code)
        logger.info("WebSocket closed with code %s", code)

    except Exception:
        pass


# ---------------------------
# 2) Common handler utilities
# ---------------------------

async def receive_chat_request(websocket: WebSocket) -> Optional[ChatRequest]:
    """
    Receives and validates client JSON.
    Returns ChatRequest or None if invalid (and sends error to client).
    """
    try:
        data = await websocket.receive_json()
        req = ChatRequest.model_validate(data)  # Pydantic v2
        logger.info("Received request: %s", req)

        return req
    
    except WebSocketDisconnect:
        logger.info("WebSocket disconnected.")
        raise

    except ValidationError as ve:
        await websocket.send_json({
            "type": "error",
            "code": "INVALID_PAYLOAD",
            "detail": ve.errors(),
            "example": {"message": "Hi"}
        })
        logger.error("Validation error: %s", ve.errors())
        return None
    
    except Exception as e:
        await websocket.send_json({
            "type": "error",
            "code": "BAD_REQUEST",
            "detail": str(e),
            "example": {"message": "Hi"}
        })
        logger.error("Error receiving request: %s", e)
        return None


# ---------------------------
# 3) Endpoint 01 (Non-stream)
# ---------------------------
@app.websocket("/ws/chat")
async def ws_chat_non_stream(websocket: WebSocket):
    await websocket.accept()
    conn_id = str(uuid.uuid4())[:8]
    logger.info("WS connected /ws/chat conn_id=%s", conn_id)

    # Basic per-connection rate limit
    # (Allows 1 request at a time; prevents flooding)
    lock = asyncio.Lock()

    try:
        while True:
            # 1) Receive and validate request
            req = await receive_chat_request(websocket)
            if req is None:
                continue

            async with lock:
                request_id = str(uuid.uuid4())[:8]
                try:
                    # Timeout on LLM call (avoid hanging)
                    result = await asyncio.wait_for(llm.ainvoke(req.message), timeout=60)

                    reply_text = to_text(result)

                    await websocket.send_json({
                        "type": "response",
                        "request_id": request_id,
                        "value": reply_text
                    })

                except asyncio.TimeoutError:
                    await websocket.send_json({
                        "type": "error",
                        "code": "LLM_TIMEOUT",
                        "request_id": request_id,
                        "detail": "LLM call exceeded 60 seconds."
                    })

                except Exception as e:
                    logger.exception("LLM error conn_id=%s request_id=%s", conn_id, request_id)
                    await websocket.send_json({
                        "type": "error",
                        "code": "LLM_ERROR",
                        "request_id": request_id,
                        "detail": str(e)
                    })

    except WebSocketDisconnect:
        logger.info("WS disconnected /ws/chat conn_id=%s", conn_id)

    except Exception:
        logger.exception("WS fatal error /ws/chat conn_id=%s", conn_id)

    finally:
        await safe_close(websocket, code=status.WS_1000_NORMAL_CLOSURE)




# ---------------------------
# 4) Endpoint 02 (Streaming)
# ---------------------------
@app.websocket("/ws/chat2")
async def ws_chat_stream(websocket: WebSocket):
    await websocket.accept()
    conn_id = str(uuid.uuid4())[:8]
    logger.info("WS connected /ws/chat2 conn_id=%s", conn_id)

    lock = asyncio.Lock()  # 1 generation at a time per connection

    try:
        while True:
            req = await receive_chat_request(websocket)
            if req is None:
                continue

            async with lock:
                request_id = str(uuid.uuid4())[:8]
                await websocket.send_json({"type": "start", "request_id": request_id})

                full_text = ""

                try:
                    # Optional overall timeout for streaming session
                    async def stream_tokens():
                        nonlocal full_text
                        async for chunk in llm.astream(req.message):
                            token = to_text(chunk)
                            if not token:
                                continue

                            full_text += token

                            # If client is gone, send_json will throw
                            await websocket.send_json({
                                "type": "chunk",
                                "request_id": request_id,
                                "value": token
                            })

                    await asyncio.wait_for(stream_tokens(), timeout=120)

                    await websocket.send_json({
                        "type": "end",
                        "request_id": request_id,
                        "full": full_text
                    })

                except asyncio.TimeoutError:
                    await websocket.send_json({
                        "type": "error",
                        "code": "STREAM_TIMEOUT",
                        "request_id": request_id,
                        "detail": "Streaming exceeded 120 seconds."
                    })

                except WebSocketDisconnect:
                    raise  # handled by outer except

                except Exception as e:
                    logger.exception("Streaming error conn_id=%s request_id=%s", conn_id, request_id)
                    # Best effort error message (client may already be gone)
                    try:
                        await websocket.send_json({
                            "type": "error",
                            "code": "STREAM_ERROR",
                            "request_id": request_id,
                            "detail": str(e)
                        })
                    except Exception:
                        pass

    except WebSocketDisconnect:
        logger.info("WS disconnected /ws/chat2 conn_id=%s", conn_id)

    except Exception:
        logger.exception("WS fatal error /ws/chat2 conn_id=%s", conn_id)

    finally:
        await safe_close(websocket, code=status.WS_1000_NORMAL_CLOSURE)

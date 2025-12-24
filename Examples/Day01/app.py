from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from llm import llm
import logging


logger = logging.getLogger("ws-chat")


# Create FastAPI APP:
app = FastAPI(
    title="Hirect",
    description="FastAPI WebSocket",
    version="1.0.0",
    docs_url="/swagger",
    redoc_url="/redoc",
)




# WebSocket Endpoint 01: Chat with LLM
@app.websocket("/ws/chat")
async def ws_chat(websocket: WebSocket):
    await websocket.accept()
    logger.info("WebSocket connected")

    try:
        while True:
            # 1) Receive JSON from Postman
            data = await websocket.receive_json()
            user_text = data.get("message", "")

            if not isinstance(user_text, str) or not user_text.strip():
                await websocket.send_text("Invalid payload. Send: {'message': 'Data must be dictonary with non-empty string value.'}")
                continue

            # 2) Call LLM
            result = await llm.ainvoke(user_text)

            # 3) Convert result to plain string safely
            if hasattr(result, "content"):
                reply_text = result.content
            else:
                reply_text = str(result)

            # 4) Send string back
            await websocket.send_text(reply_text)

    except WebSocketDisconnect:
        logger.info("WebSocket disconnected by client")

    except Exception as e:
        logger.exception("WebSocket error: %s", e)
        try:
            await websocket.close(code=1011)

        except Exception:
            pass



# WebSocket Endpoint 02: Chat with LLM (Streaming)
@app.websocket("/ws/chat2")
async def ws_chat(websocket: WebSocket):
    await websocket.accept()
    logger.info("WebSocket connected")

    try:
        while True:
            # 1) Receive JSON from Postman
            data = await websocket.receive_json()
            user_text = data.get("message", "")

            if not isinstance(user_text, str) or not user_text.strip():
                await websocket.send_json({
                    "type": "error",
                    "message": "Invalid payload. Send: {'message': '<non-empty string>'}"
                })
                continue

            # 2) Tell client streaming is starting
            await websocket.send_json({"type": "start"})

            # 3) Stream tokens/chunks from LLM
            full_text = ""
            async for chunk in llm.astream(user_text):
                # chunk can be str OR AIMessageChunk OR other chunk type
                if hasattr(chunk, "content"):
                    token = chunk.content
                else:
                    token = str(chunk)

                # Some providers send empty/None chunks
                if not token:
                    continue

                full_text += token

                # Send incremental chunk to client
                await websocket.send_json({
                    "type": "chunk",
                    "value": token
                })

            # 4) Tell client streaming finished (optionally include full text)
            await websocket.send_json({
                "type": "end",
                "full": full_text
            })

    except WebSocketDisconnect:
        logger.info("WebSocket disconnected by client")

    except Exception as e:
        logger.exception("WebSocket error: %s", e)
        try:
            await websocket.close(code=1011)
        except Exception:
            pass
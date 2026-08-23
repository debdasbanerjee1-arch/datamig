"""API delivery layer.

The FastAPI application that exposes the engine over HTTP (JSON + SSE) and
serves the web/ client. Imports from `engine`; never the reverse.

    uvicorn api.server:app --reload
"""

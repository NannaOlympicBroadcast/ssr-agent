"""Minimal JSON-RPC 2.0 helpers shared by the bus server and client.

The bus speaks structured JSON-RPC 2.0 over a message transport (WebSocket text
frames). These helpers build/parse the envelopes so the server and client agree
on the wire format and on a small set of error codes.
"""

from __future__ import annotations

import json

# Standard JSON-RPC 2.0 error codes plus a couple of bus-specific ones.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
BUS_ERROR = -32000
UNAUTHORIZED = -32001  # missing / invalid bus api key


class JsonRpcError(Exception):
    """Raised by method handlers to return a structured JSON-RPC error."""

    def __init__(self, code: int, message: str, data=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


def request(method: str, params: dict | None = None, id=None) -> dict:
    msg = {"jsonrpc": "2.0", "method": method, "params": params or {}}
    if id is not None:
        msg["id"] = id
    return msg


def notification(method: str, params: dict | None = None) -> dict:
    return {"jsonrpc": "2.0", "method": method, "params": params or {}}


def result(id, value) -> dict:
    return {"jsonrpc": "2.0", "id": id, "result": value}


def error(id, code: int, message: str, data=None) -> dict:
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": id, "error": err}


def dumps(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False)


def loads(raw: str) -> dict:
    obj = json.loads(raw)
    if not isinstance(obj, dict):
        raise ValueError("JSON-RPC message must be an object")
    return obj

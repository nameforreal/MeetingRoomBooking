"""Explicit OpenAPI contract for the current API; no schema-generation dependency."""

from django.http import HttpResponse, JsonResponse
from django.views.decorators.http import require_GET


def ref(name):
    return {"$ref": "#/components/schemas/" + name}


def body(schema):
    return {"required": True, "content": {"application/json": {"schema": schema}}}


def response(schema, description="Success"):
    return {"description": description, "content": {"application/json": {"schema": schema}}}


def query(name, kind="string", **extra):
    return {"name": name, "in": "query", "schema": {"type": kind, **extra}}


ID = {"name": "id", "in": "path", "required": True, "schema": {"type": "integer", "minimum": 1}}
DATE = {"type": "string", "format": "date-time"}
ROOM_FIELDS = {
    "name": {"type": "string"},
    "description": {"type": "string"},
    "capacity": {"type": "integer"},
    "is_vip": {"type": "boolean"},
    "is_active": {"type": "boolean"},
}
CREATE_FIELDS = {"room": {"type": "integer", "minimum": 1}, "start_time": DATE, "end_time": DATE}
UPDATE_FIELDS = CREATE_FIELDS | {"revision": {"type": "integer", "minimum": 0}}
SCHEMAS = {
    "RoomInput": {"type": "object", "properties": ROOM_FIELDS, "required": ["name"]},
    "Room": {
        "type": "object",
        "properties": ROOM_FIELDS | {"id": {"type": "integer"}, "created_at": DATE},
    },
    "BookingCreate": {
        "type": "object",
        "properties": CREATE_FIELDS,
        "required": list(CREATE_FIELDS),
    },
    "BookingUpdate": {
        "type": "object",
        "properties": UPDATE_FIELDS,
        "required": list(UPDATE_FIELDS),
    },
    "BookingPatch": {"type": "object", "properties": UPDATE_FIELDS, "required": ["revision"]},
    "Revision": {
        "type": "object",
        "properties": {"revision": {"type": "integer", "minimum": 0}},
        "required": ["revision"],
    },
    "Booking": {
        "type": "object",
        "properties": UPDATE_FIELDS
        | {
            "id": {"type": "integer"},
            "room_name": {"type": "string"},
            "status": {"type": "string", "enum": ["pending", "confirmed", "cancelled"]},
            "status_display": {"type": "string"},
            "created_at": DATE,
        },
    },
    "Availability": {
        "type": "object",
        "properties": {
            "room": {"type": "integer"},
            "room_name": {"type": "string"},
            "date": {"type": "string", "format": "date"},
            "busy_intervals": {
                "type": "array",
                "items": {"type": "object", "properties": {"start_time": DATE, "end_time": DATE}},
            },
        },
    },
    "Error": {
        "description": "Field errors, booking errors, or {code, detail}; stale revision currently returns 400.",
        "type": "object",
        "additionalProperties": True,
    },
}
ERRORS = {
    str(code): response(ref("Error"), description)
    for code, description in [
        (400, "Invalid data / revision"),
        (403, "Authentication, CSRF or permission denied"),
        (404, "Not found or not owned"),
    ]
}
PATHS = {
    "/api/rooms/": {
        "get": {
            "security": [],
            "summary": "Active rooms (array, currently unpaginated)",
            "parameters": [query("include_archived", enum=["true", "false"])],
            "responses": {"200": response({"type": "array", "items": ref("Room")}), **ERRORS},
        },
        "post": {
            "summary": "Staff with rooms.add_room",
            "requestBody": body(ref("RoomInput")),
            "responses": {"201": response(ref("Room")), **ERRORS},
        },
    },
    "/api/rooms/{id}/": {
        "parameters": [ID],
        "get": {"security": [], "responses": {"200": response(ref("Room")), **ERRORS}},
        "put": {
            "summary": "Staff with rooms.change_room",
            "requestBody": body(ref("RoomInput")),
            "responses": {"200": response(ref("Room")), **ERRORS},
        },
        "patch": {
            "summary": "Staff with rooms.change_room",
            "requestBody": body({"type": "object", "properties": ROOM_FIELDS}),
            "responses": {"200": response(ref("Room")), **ERRORS},
        },
        "delete": {
            "summary": "Archive room; preserves history (rooms.change_room)",
            "responses": {"204": {"description": "Archived"}, **ERRORS},
        },
    },
    "/api/rooms/{id}/availability/": {
        "parameters": [ID],
        "get": {
            "security": [],
            "parameters": [query("date", format="date") | {"required": True}],
            "responses": {"200": response(ref("Availability")), **ERRORS},
        },
    },
    "/api/bookings/": {
        "get": {
            "summary": "Own bookings (array, currently unpaginated)",
            "parameters": [
                query("room", "integer"),
                query("status", enum=["pending", "confirmed", "cancelled"]),
                query("date_from", format="date"),
                query("date_to", format="date"),
                query("period", enum=["upcoming", "past"]),
                query("ordering", enum=["start_time", "-start_time", "created_at", "-created_at"]),
            ],
            "responses": {"200": response({"type": "array", "items": ref("Booking")}), **ERRORS},
        },
        "post": {
            "parameters": [
                {
                    "name": "Idempotency-Key",
                    "in": "header",
                    "required": True,
                    "schema": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 128,
                        "pattern": "^[A-Za-z0-9._:-]+$",
                    },
                }
            ],
            "requestBody": body(ref("BookingCreate")),
            "responses": {
                "201": response(
                    ref("Booking"), "Initial snapshot, also on replay; Idempotency-Replayed header"
                ),
                "409": response(ref("Error"), "Key reused with different data"),
                "503": response(ref("Error"), "In progress; Retry-After header"),
                **ERRORS,
            },
        },
    },
    "/api/bookings/{id}/": {
        "parameters": [ID],
        "get": {"responses": {"200": response(ref("Booking")), **ERRORS}},
        "put": {
            "requestBody": body(ref("BookingUpdate")),
            "responses": {"200": response(ref("Booking")), **ERRORS},
        },
        "patch": {
            "requestBody": body(ref("BookingPatch")),
            "responses": {"200": response(ref("Booking")), **ERRORS},
        },
    },
    "/api/bookings/{id}/cancel/": {
        "parameters": [ID],
        "post": {
            "summary": "Cancel own booking; repeated cancellation is idempotent",
            "requestBody": body(ref("Revision")),
            "responses": {"200": response(ref("Booking")), **ERRORS},
        },
    },
}
SPEC = {
    "openapi": "3.0.3",
    "info": {
        "title": "MeetingRoomBooking",
        "version": "1.0.0",
        "description": "Session authentication: login at /login/. Unsafe requests also require X-CSRFToken and csrftoken cookie. Only the owner can access a booking; staff status does not broaden this API. VIP approval is available in administrative interfaces, not a public API action. See API_GUIDE.md for date/time limits.",
    },
    "security": [{"session": []}],
    "paths": PATHS,
    "components": {
        "securitySchemes": {"session": {"type": "apiKey", "in": "cookie", "name": "sessionid"}},
        "schemas": SCHEMAS,
    },
}


@require_GET
def schema(request):
    return JsonResponse(SPEC)


@require_GET
def docs(request):
    return HttpResponse(
        """<!doctype html><html lang="ru"><meta charset="utf-8"><title>MeetingRoomBooking API</title><link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5.17.14/swagger-ui.css"><p><a href="/api/schema/">OpenAPI JSON (доступен без CDN)</a>. Войдите через /login/; unsafe-запросы требуют CSRF.</p><div id="swagger"></div><script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5.17.14/swagger-ui-bundle.js"></script><script>SwaggerUIBundle({url:'/api/schema/',dom_id:'#swagger',supportedSubmitMethods:[]});</script></html>"""
    )

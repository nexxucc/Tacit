# API, schema and evaluator compatibility

How clients and the backend declare and enforce wire-contract versions.

[Project overview](README.md) · [Setup](GETTING_STARTED.md) · [Verification](VERIFICATION.md)

This policy versions the client/backend wire contract separately from the Python package release and PostgreSQL migration revision. The shared source of truth is `packages/tacit_contracts/src/tacit_contracts/versions.py`.

| Declaration | Current value | Meaning |
|---|---|---|
| `X-Tacit-API-Version` | `1` | The `/v1` endpoint and transport contract |
| `X-Tacit-Schema-Version` | `1` | Request/response JSON and durable voice-upload framing contract |
| `X-Tacit-Evaluator-Version` | `typed-expression-v2` | Deterministic rule semantics, including legacy equality predicates and the bounded expression AST |

Version tokens are exact, case-sensitive identifiers. These are declarations of implemented compatibility, not a request to translate or downgrade a payload. A different token, an empty declaration, a comma-separated version list or a duplicated version header is incompatible. The supported set currently contains one value in each dimension.

## Server behavior

The API inspects explicit declarations before reading an HTTP request body, authenticating, resolving repository access, creating a device pairing, accepting a voice WebSocket, reserving a model operation or invoking a provider. Any declared incompatible version returns HTTP **426** with the normal JSON error envelope:

```json
{
  "code": "upgrade_required",
  "message": "Incompatible Tacit schema version. Supported versions: API 1, schema 1, evaluator typed-expression-v2. Upgrade the Tacit client and backend together; the request was not automatically retried.",
  "request_id": "generated-request-id",
  "retryable": false
}
```

This status means that the Tacit application contracts must be upgraded together. Retrying the same incompatible request cannot fix it. The response declares the backend's three supported version headers; it does not perform a protocol downgrade or bypass normal authorization.

Handled HTTP responses, including readiness, authorization errors, validation errors, audio downloads and compatibility refusals, declare all three version headers. Successful WebSocket handshakes declare them as well. An incompatible socket is denied with the same HTTP 426 envelope when the ASGI server supports WebSocket denial responses. On an ASGI server without that extension, it is closed before acceptance with policy code `1008`; an HTTP-only handshake observer can see a generic refusal instead of the JSON envelope. No audio frames are accepted in either case. Production should use the configured Uvicorn transport with denial-response support.

Uncaught infrastructure failures may be produced outside the application middleware or by a reverse proxy; their version headers are not a substitute for a successful API response. Proxies must preserve all three headers in requests and responses. They must not fabricate current version headers on unrelated upstream responses.

## SDK, CLI and MCP behavior

`TacitClient` sends all three declarations for JSON HTTP operations, streamed audio and voice WebSocket handshakes. The CLI's unauthenticated device-start/device-poll requests send the same declarations before a credential exists. A compatibility failure is displayed as a readable `CompatibilityError`, including the client's supported versions and an upgrade instruction; it is not reduced to a generic connectivity failure. Audio is not consumed and socket upload frames are not sent after an incompatible response is detected. Retained local audio remains available for recovery with a compatible backend.

The SDK validates declared response headers before decoding JSON, reading an audio stream or yielding an accepted voice socket. If the server declares any version header, it must declare the complete matching set. Mismatches, duplicate declarations and partial header sets are refused. An explicitly reported `evaluator_version` in a JSON response must also equal the shared current evaluator version; contradictory body/header declarations are not silently accepted.

The stdio MCP adapter uses this SDK for its backend operations, so its context, question, history and evaluation tools use the same negotiation and errors. This does not version the external MCP specification itself or grant an agent any new permissions.

## Legacy and upgrade policy

For the existing development installation, requests without version declarations remain accepted and pass through normal schema validation and authorization. Partially declared legacy requests are accepted only when every supplied value matches. GitHub webhooks and manually operated older clients therefore continue to work without inventing headers. Versionless requests do not establish that an arbitrary old payload is compatible: malformed bodies still receive `unsupported_schema` (HTTP 422).

For staged rollout, the SDK also accepts responses with **none** of these headers from older backends. It still checks an explicitly declared JSON `evaluator_version`. A response with only some headers is not treated as a versionless legacy response. The absence of all response headers means compatibility is unadvertised, not verified; this bridge should be removed only in a separately announced breaking release after clients and backends have been upgraded.

Deploy a matching contracts/SDK/CLI/MCP/API stack from the same pinned release. Migrate and back up the database using the separate migration procedure; changing wire headers never migrates stored rows. Preserve existing stable credential-encryption keys and retained recording metadata. Incompatible requests must not trigger automatic login, transcription, inference, publication or upload replay. Once a compatible release is running, inspect the retained operation/session and use its existing recovery/idempotency workflow rather than silently allocating a new paid operation.

A future API or schema breaking change requires a new shared token and an explicit rollout/migration policy. Evaluator semantic changes require a new evaluator identifier even if HTTP paths and JSON shapes do not change. Do not add an older token to a supported set unless that behavior is actually implemented; do not label current expression evaluation as `typed-equality-v1` merely to satisfy an older client.

This implementation has not been tested or checked in the current development stretch, at the user's request. HTTP, audio, device login, proxy/header preservation and real WebSocket/MCP compatibility acceptance remain work for the later verification stage.

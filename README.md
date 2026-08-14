# v4w-submit

Cloud Run service that submits a subscriber callback request to the Vets4Warriors
Ninja Forms intake form on their behalf, and returns a clean success/failure
result.

The Vets4Warriors form is a Ninja Forms (WordPress) form, whose AJAX submission
endpoint validates a per-render WordPress nonce. A server-to-server caller has no
browser session to carry that nonce, so this service uses Ninja Forms' built-in
"Just-in-Time" nonce path: it POSTs once with an invalid nonce, reads the fresh
nonce Ninja Forms returns, and re-POSTs with it. Both POSTs happen back-to-back
in a single request, so the nonce is always seconds old.

This keeps all of the form-specific detail (the field envelope, the two-step
nonce handshake, phone/name normalization) in one place. Callers send a small
flat JSON body and get back `{success, sub_id}`.

## Endpoints

### `GET /health`
Health check. Returns `{"status": "ok"}`.

### `POST /submit`

**Request:**
```json
{
    "password": "...",
    "name": "Jane Doe",
    "phone": "+13075551234",
    "zip": "85003",
    "gender": "Female",
    "branch": "Army"
}
```

- `name` may be sent as a single full name (split on the first space) or as
  separate `fname` / `lname` fields.
- `phone` accepts any format; it is reduced to the bare 10-digit number the form
  expects.
- `gender` is optional and defaults to `Prefer not to Disclose`.
- `branch` is optional and defaults to `Unknown`.
- `zip`, first name, and phone are required.

**Response (HTTP 200):**
```json
{
    "status": "success",
    "success": true,
    "sub_id": "813796",
    "detail": "created on POST2"
}
```

- On a form-side rejection: HTTP 200 with `success: false`; `detail` carries the
  cause (the failure is also logged at error level).
- On bad auth: HTTP 403.
- On a transport failure reaching the form: HTTP 502.

## Configuration

All configuration is via environment variables — no secrets are committed.

| Variable | Purpose |
|---|---|
| `V4W_SUBMIT_PASSWORD` | Shared secret required in the request body. |
| `NF_SUBMIT_URL` | Ninja Forms AJAX submit endpoint (has a sensible default). |

## Notes

Ninja Forms returns HTTP 200 on both the nonce-retry response and a genuine
success, so success is determined by the response body (`data.actions.save.sub_id`
present), never by the status code alone.

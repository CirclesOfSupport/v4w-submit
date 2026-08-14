import os
import json
import logging
import urllib.parse

import requests
from flask import Flask, request, jsonify

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
# Shared-secret password, same convention as sheet-service. Set via env var;
# never hardcoded (these repos are PUBLIC).
PASSWORD = os.environ.get("V4W_SUBMIT_PASSWORD", "")

# ---------------------------------------------------------------------------
# Vets4Warriors Ninja Forms endpoint + form contract
# ---------------------------------------------------------------------------
# The form V4W stood up for Early Alert (2026-07-30): Ninja Forms, WordPress.
#   Public page:  https://vets4warriors.com/ninja-forms/383pwa/
#   Submit route: POST https://vets4warriors.com/wp-admin/admin-ajax.php
#                 action=nf_ajax_submit
#   Form id:      "38"  (rendered instance id is "38_1" — MUST be sent as 38_1,
#                 with all field keys _1-suffixed, or NF's instance-fix drops the
#                 first field's value and returns a spurious required-error)
#
# NONCE: NF validates a WordPress nonce (param "security"). A stateless caller
# has no browser session to scrape a fresh nonce, BUT the controller implements
# a "Just-in-Time" nonce path: POST with an invalid nonce and NF returns
# HTTP 200 with body {"errors":{"nonce":{"new_nonce":"...","nonce_ts":...}}}.
# Re-POST with security=new_nonce & nonce_ts=<ts> and the submission goes
# through — no cookies, no prior GET required. Confirmed 2026-08-14 (sub_id
# 813796 created via two stateless POSTs). This service performs both POSTs
# back-to-back, so the nonce is always seconds old.
#
# SUCCESS: NF returns HTTP 200 on BOTH the nonce-retry AND real success, so the
# HTTP status cannot distinguish them. Success is identified by the BODY:
# data.actions.save.sub_id present and errors empty/absent.

NF_SUBMIT_URL = os.environ.get(
    "NF_SUBMIT_URL", "https://vets4warriors.com/wp-admin/admin-ajax.php"
)
NF_FORM_INSTANCE_ID = "38_1"

# Static consent HTML display fields (exact strings the real form emits). NF
# echoes these back as field values on a genuine browser submit; they are
# display-only ("html" field type) but are included to match the known-good
# wire payload byte-for-byte.
_HTML_PHONE = ("<p>I agree to receive phone calls from Vets4Warriors on my mobile "
               "device. Mobile and data charges may apply.</p>")
_HTML_EMAIL = ("<p>I agree to receive emails from Vets4Warriors. I can opt out at "
               "any time by clicking unsubscribe in email communication or entering "
               "unsubscribe in subject of email.</p>")
_HTML_TEXT = ("<p>I agree to receive text messages from Vets4Warriors. Message "
              "&amp; data rates may apply. Message frequency varies. Reply STOP to "
              "cancel.</p>")
_HTML_TOS = ('<p><a href="https://vets4warriors.com/privacy-policy/" rel="noopener '
             'noreferrer" target="_blank">Terms of Service and Privacy Policy</a></p>')

# Request/connect timeouts (seconds) for each leg to V4W.
NF_TIMEOUT = (10, 30)

# Default UA — some WAF configs reject empty/scripted UAs.
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/127.0 Safari/537.36")


def check_password(body: dict) -> bool:
    return body.get("password") == PASSWORD


def parse_body_or_400():
    """Parse JSON body, distinguishing unparseable from empty (sheet-service
    pattern — a malformed body must 400, not masquerade as a 403)."""
    body = request.get_json(force=True, silent=True)
    if body is None:
        return {}, (jsonify({"status": "error", "message": "Invalid JSON"}), 400)
    return body, None


def build_form_data(fields_values: dict) -> str:
    """Build the NF formData JSON envelope from resolved subscriber values.

    fields_values keys: fname, lname, phone, zip, gender, branch (all strings;
    caller has already defaulted/normalized them).

    Returns the JSON string (NOT url-encoded — caller url-encodes when placing
    it in the form-urlencoded body).
    """
    def f(fid, value):
        return {"value": value, "id": fid}

    fields = {
        "453_1": f("453_1", fields_values["fname"]),
        "454_1": f("454_1", fields_values["lname"]),
        "451_1": f("451_1", ""),                      # email (not sent)
        "455_1": f("455_1", fields_values["phone"]),
        "456_1": f("456_1", fields_values["zip"]),
        "457_1": f("457_1", fields_values["gender"]),
        "458_1": f("458_1", fields_values["branch"]),
        "459_1": {"value": [], "id": "459_1"},        # Era (multiselect) -> []
        "460_1": f("460_1", "Did not obtain"),        # Duty status
        "462_1": {"value": 1, "id": "462_1"},         # permission to call mobile (required)
        "466_1": f("466_1", _HTML_PHONE),
        "461_1": {"value": 1, "id": "461_1"},         # permission to leave a message
        "463_1": {"value": 0, "id": "463_1"},         # permission to email
        "467_1": f("467_1", _HTML_EMAIL),
        "464_1": {"value": 0, "id": "464_1"},         # permission to text mobile
        "468_1": f("468_1", _HTML_TEXT),
        "465_1": f("465_1", _HTML_TOS),
        "452_1": f("452_1", ""),                      # submit field
    }
    envelope = {"id": NF_FORM_INSTANCE_ID, "fields": fields, "extra": {}}
    return json.dumps(envelope, ensure_ascii=False)


def _nf_post(form_data_json: str, security: str, nonce_ts: str | None):
    """POST the NF envelope once. Returns the parsed JSON body (dict) and the
    raw requests.Response. formData is url-encoded here (form-urlencoded body)."""
    payload = {
        "action": "nf_ajax_submit",
        "security": security,
        "formData": form_data_json,
        "formId": NF_FORM_INSTANCE_ID,
    }
    if nonce_ts is not None:
        payload["nonce_ts"] = nonce_ts

    resp = requests.post(
        NF_SUBMIT_URL,
        data=payload,  # requests url-encodes form fields, incl. the formData JSON
        headers={
            "User-Agent": UA,
            "X-Requested-With": "XMLHttpRequest",
            "Referer": "https://vets4warriors.com/ninja-forms/383pwa/",
        },
        timeout=NF_TIMEOUT,
    )
    try:
        parsed = resp.json()
    except ValueError:
        parsed = None
    return parsed, resp


def _extract_sub_id(parsed: dict):
    """Pull data.actions.save.sub_id, tolerating NF's shape variation.

    On a genuine success NF returns data as an OBJECT:
      {"data":{"actions":{"save":{"sub_id":813796}}}, ...}
    On the nonce-retry (and some error) responses NF returns data as an empty
    LIST:  {"data":[], "errors":{...}}.  Calling .get() on that list raises
    AttributeError, which is what produced the 500. Guard every hop: if any
    level isn't a dict, there's no sub_id.
    """
    data = parsed.get("data")
    if not isinstance(data, dict):
        return None
    actions = data.get("actions")
    if not isinstance(actions, dict):
        return None
    save = actions.get("save")
    if not isinstance(save, dict):
        return None
    return save.get("sub_id")


def submit_to_v4w(fields_values: dict) -> dict:
    """Full two-POST JiT-nonce submission. Returns a normalized result dict:
      {"success": bool, "sub_id": <str|None>, "detail": <str>}
    Never raises for a V4W-side rejection — those come back as success=False
    with detail. Raises only on transport failure (caller maps to 502)."""

    form_data_json = build_form_data(fields_values)

    # PII-safe request marker: never log the full phone (veteran PII). Last 4
    # is enough to correlate a run with the TextIt httplog / a V4W record.
    phone = fields_values.get("phone", "")
    phone_tail = phone[-4:] if len(phone) >= 4 else phone
    logger.info("V4W submit start: name=%s %s zip=%s phone=...%s gender=%s branch=%s",
                fields_values.get("fname"), fields_values.get("lname"),
                fields_values.get("zip"), phone_tail,
                fields_values.get("gender"), fields_values.get("branch"))

    # POST 1: intentionally invalid nonce to trigger the JiT nonce response.
    p1, r1 = _nf_post(form_data_json, security="0", nonce_ts=None)

    if p1 is None:
        return {"success": False, "sub_id": None,
                "detail": f"POST1 non-JSON response (HTTP {r1.status_code})"}

    # If POST1 somehow already succeeded (nonce happened to validate), take it.
    sub_id = _extract_sub_id(p1)
    if sub_id:
        return {"success": True, "sub_id": str(sub_id), "detail": "created on POST1"}

    nonce_block = (p1.get("errors", {}) or {}).get("nonce")
    if not nonce_block or not nonce_block.get("new_nonce"):
        # No JiT nonce came back and no success — a real validation/other error.
        return {"success": False, "sub_id": None,
                "detail": f"POST1 no JiT nonce; errors={json.dumps(p1.get('errors'))}"}

    new_nonce = nonce_block["new_nonce"]
    nonce_ts = str(nonce_block.get("nonce_ts", ""))
    logger.info("V4W POST1 returned JiT nonce (ts=%s); retrying", nonce_ts)

    # POST 2: resubmit with the JiT nonce.
    p2, r2 = _nf_post(form_data_json, security=new_nonce, nonce_ts=nonce_ts)

    if p2 is None:
        return {"success": False, "sub_id": None,
                "detail": f"POST2 non-JSON response (HTTP {r2.status_code})"}

    sub_id = _extract_sub_id(p2)
    if sub_id:
        logger.info("V4W submit success: sub_id=%s (POST2)", sub_id)
        return {"success": True, "sub_id": str(sub_id), "detail": "created on POST2"}

    # Still no sub_id: surface whatever NF said (field errors, nonce loop, etc.)
    errors = p2.get("errors")
    if errors and errors.get("nonce"):
        return {"success": False, "sub_id": None,
                "detail": "POST2 nonce-blocked (JiT loop) — form likely hardened"}
    return {"success": False, "sub_id": None,
            "detail": f"POST2 no sub_id; errors={json.dumps(errors)}"}


# ---------------------------------------------------------------------------
# Field normalization
# ---------------------------------------------------------------------------

# Gender picklist on the V4W form. If the subscriber value doesn't match exactly,
# NF stores raw text or nulls it. Default when absent/unmatched:
GENDER_DEFAULT = "Prefer not to Disclose"
BRANCH_DEFAULT = "Unknown"


def normalize_fields(body: dict) -> dict:
    """Resolve the flat inbound body into the NF field values.

    Inbound (from TextIt, clean JSON):
      name  (full name; split on first space)  OR  fname + lname
      phone (any format; reduced to bare 10 digits)
      zip
      gender   (optional)
      branch   (optional; a.k.a. militarybranch)
    """
    # Name: prefer explicit fname/lname; else split full name on first space.
    fname = (body.get("fname") or "").strip()
    lname = (body.get("lname") or "").strip()
    if not fname and body.get("name"):
        parts = str(body["name"]).strip().split(" ", 1)
        fname = parts[0]
        lname = parts[1] if len(parts) > 1 else ""

    # Phone: strip to the last 10 digits (NF phone field expects bare 10-digit).
    raw_phone = str(body.get("phone", ""))
    digits = "".join(ch for ch in raw_phone if ch.isdigit())
    phone = digits[-10:] if len(digits) >= 10 else digits

    zipc = str(body.get("zip", "")).strip()

    gender = str(body.get("gender", "")).strip() or GENDER_DEFAULT
    branch = str(body.get("branch", body.get("militarybranch", ""))).strip() or BRANCH_DEFAULT

    return {
        "fname": fname,
        "lname": lname,
        "phone": phone,
        "zip": zipc,
        "gender": gender,
        "branch": branch,
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


@app.route("/submit", methods=["POST"])
def submit():
    """Submit a callback request to the Vets4Warriors Ninja Forms endpoint.

    Request body (JSON):
    {
        "password": "...",
        "name": "Jane Doe",          // or "fname"/"lname" separately
        "phone": "+13075551234",     // any format; reduced to 10 digits
        "zip": "85003",
        "gender": "Female",          // optional; defaults to "Prefer not to Disclose"
        "branch": "Army"             // optional; defaults to "Unknown"
    }

    Response (always HTTP 200 unless auth/transport fails):
    {
        "status": "success",
        "success": true,
        "sub_id": "813796",
        "detail": "created on POST2"
    }

    On a V4W-side rejection: HTTP 200, success=false, detail carries the cause.
    On bad auth: 403. On transport failure to V4W: 502.
    """
    body, err = parse_body_or_400()
    if err:
        return err

    if not check_password(body):
        return jsonify({"status": "error", "message": "Unauthorized"}), 403

    fields_values = normalize_fields(body)

    # Minimal required-field guard (NF requires first/last/phone/zip + call perm).
    missing = [k for k in ("fname", "phone", "zip") if not fields_values.get(k)]
    if missing:
        return jsonify({
            "status": "error",
            "message": f"missing required field(s): {', '.join(missing)}",
        }), 400

    try:
        result = submit_to_v4w(fields_values)
    except requests.RequestException as e:
        logger.exception("Transport error submitting to V4W")
        return jsonify({"status": "error", "success": False,
                        "message": f"V4W transport error: {e}"}), 502
    except Exception as e:
        logger.exception("Unexpected error submitting to V4W")
        return jsonify({"status": "error", "success": False,
                        "message": f"internal error: {e}"}), 500

    if not result["success"]:
        # Log loudly — this is the case that must be visible when it fails.
        logger.error("V4W submission failed: %s | fields=%s",
                     result["detail"],
                     {k: fields_values[k] for k in ("fname", "lname", "zip")})

    return jsonify({
        "status": "success" if result["success"] else "error",
        "success": result["success"],
        "sub_id": result["sub_id"],
        "detail": result["detail"],
    }), 200


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

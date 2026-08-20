import os
import re
import json
import logging
import datetime

import requests
from flask import Flask, request, jsonify
from google.cloud import tasks_v2
from google.protobuf import timestamp_pb2

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
# Shared-secret password, same convention as sheet-service. Set via env var;
# never hardcoded (these repos are PUBLIC). The SAME shared secret authenticates
# both this service's endpoints AND the sheet-service /write call (the TextIt
# global @globals.gappscriptapi is used for both).
PASSWORD = os.environ.get("V4W_SUBMIT_PASSWORD", "")

# ---------------------------------------------------------------------------
# Vets4Warriors Ninja Forms endpoint + form contract
# ---------------------------------------------------------------------------
# See repo history / Atlas itdo_377_vets4warriors.md for the full NF mechanism
# writeup (JiT nonce, base instance "38", body-distinguishable success).

NF_SUBMIT_URL = os.environ.get(
    "NF_SUBMIT_URL", "https://vets4warriors.com/wp-admin/admin-ajax.php"
)
NF_FORM_INSTANCE_ID = "38"

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

NF_TIMEOUT = (10, 30)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/127.0 Safari/537.36")

# ---------------------------------------------------------------------------
# Cloud Tasks — delayed retry on a Cloudflare 403
# ---------------------------------------------------------------------------
# A 403 from V4W is a Cloudflare block on Cloud Run's rotating egress IP — it is
# NON-terminal: the same post resent minutes later rides a different pooled IP
# and typically passes. On a 403, /submit enqueues a Cloud Task that re-calls
# /retry after RETRY_DELAY_SECONDS; /retry re-attempts and, if it 403s again,
# enqueues the next retry until MAX_ATTEMPTS is reached. If TASKS_QUEUE is unset
# the enqueue is skipped and logged (service still runs, just with no retry).
TASKS_QUEUE = os.environ.get("TASKS_QUEUE", "")
TASKS_LOCATION = os.environ.get("TASKS_LOCATION", "us-east1")
TASKS_PROJECT = os.environ.get("TASKS_PROJECT", "early-alert-responses")
SERVICE_URL = os.environ.get(
    "SERVICE_URL", "https://v4w-submit-853176470965.us-east1.run.app"
)
RETRY_DELAY_SECONDS = int(os.environ.get("RETRY_DELAY_SECONDS", "300"))  # ~5 min
MAX_ATTEMPTS = int(os.environ.get("MAX_ATTEMPTS", "3"))  # 1 initial + 2 retries

# ---------------------------------------------------------------------------
# sheet-service /write — referral logging (retry-recovered + final-fail only)
# ---------------------------------------------------------------------------
# The happy path (immediate success) is logged by the TextIt flow itself. This
# service writes the Referrals row ONLY for cases the flow can't see because it
# has ended on the 403 branch: a retry that SUCCEEDS, or one that FAILS after
# MAX_ATTEMPTS. Row matches the flow's /write node (uuid / timestamp / provider)
# plus the Retry column: "SUCCESS" | "FAIL". Happy path leaves Retry blank.
SHEET_WRITE_URL = os.environ.get(
    "SHEET_WRITE_URL", "https://sheet-service-853176470965.us-east1.run.app/write"
)
SHEET_ID = os.environ.get("SHEET_ID", "1CquixL95khVlhSrzWjSGetAevFX2-_kl2RgjFe0Q1hI")
SHEET_TAB = os.environ.get("SHEET_TAB", "Referrals")
SHEET_TIMEOUT = (10, 30)


def is_403_detail(detail: str) -> bool:
    """True if a submit result detail is the Cloudflare-403 block case."""
    return bool(detail) and "403" in detail


def check_password(body: dict) -> bool:
    return body.get("password") == PASSWORD


def parse_body_or_400():
    body = request.get_json(force=True, silent=True)
    if body is None:
        return {}, (jsonify({"status": "error", "message": "Invalid JSON"}), 400)
    return body, None


def build_form_data(fields_values: dict) -> str:
    """Build the NF formData JSON envelope from resolved subscriber values."""
    def f(fid, value):
        return {"value": value, "id": fid}

    fields = {
        "451": f(451, ""),
        "452": f(452, ""),
        "453": f(453, fields_values["fname"]),
        "454": f(454, fields_values["lname"]),
        "455": f(455, fields_values["phone"]),
        "456": f(456, fields_values["zip"]),
        "457": f(457, fields_values["gender"]),
        "458": f(458, fields_values["branch"]),
        "459": {"value": [], "id": 459},
        "460": f(460, "Did not obtain"),
        "461": {"value": 1, "id": 461},
        "462": {"value": 1, "id": 462},
        "463": {"value": 0, "id": 463},
        "464": {"value": 0, "id": 464},
        "465": f(465, _HTML_TOS),
        "466": f(466, _HTML_PHONE),
        "467": f(467, _HTML_EMAIL),
        "468": f(468, _HTML_TEXT),
    }
    envelope = {"id": NF_FORM_INSTANCE_ID, "fields": fields, "extra": {}}
    return json.dumps(envelope, ensure_ascii=False)


def _nf_post(form_data_json: str, security: str, nonce_ts):
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
        data=payload,
        headers={
            "User-Agent": UA,
            "X-Requested-With": "XMLHttpRequest",
            "Referer": "https://vets4warriors.com/eatalk-to-us/",
        },
        timeout=NF_TIMEOUT,
    )
    try:
        parsed = resp.json()
    except ValueError:
        parsed = None
        body_text = resp.text or ""
        snippet = body_text[:1500].replace("\n", " ")
        ray = ""
        m = (re.search(r"[Rr]ay ID:\s*(?:<[^>]*>\s*)*([0-9a-f]{16})", body_text)
             or re.search(r'c[Rr]ay["\':\s]*([0-9a-f]{16})', body_text)
             or re.search(r"cf-ray[^0-9a-f]*([0-9a-f]{16})", body_text))
        if m:
            ray = m.group(1)
        logger.warning("V4W non-JSON response: HTTP %s | ray_id=%s | body[:1500]=%s",
                       resp.status_code, ray or "(none found)", snippet)
    return parsed, resp


def _extract_sub_id(parsed: dict):
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
    """Full two-POST JiT-nonce submission. Returns normalized result dict:
      {"success": bool, "sub_id": <str|None>, "detail": <str>}"""
    form_data_json = build_form_data(fields_values)

    phone = fields_values.get("phone", "")
    phone_tail = phone[-4:] if len(phone) >= 4 else phone
    logger.info("V4W submit start: name=%s %s zip=%s phone=...%s gender=%s branch=%s",
                fields_values.get("fname"), fields_values.get("lname"),
                fields_values.get("zip"), phone_tail,
                fields_values.get("gender"), fields_values.get("branch"))

    p1, r1 = _nf_post(form_data_json, security="0", nonce_ts=None)

    if p1 is None:
        return {"success": False, "sub_id": None,
                "detail": f"POST1 non-JSON response (HTTP {r1.status_code})"}

    sub_id = _extract_sub_id(p1)
    if sub_id:
        return {"success": True, "sub_id": str(sub_id), "detail": "created on POST1"}

    nonce_block = (p1.get("errors", {}) or {}).get("nonce")
    if not nonce_block or not nonce_block.get("new_nonce"):
        return {"success": False, "sub_id": None,
                "detail": f"POST1 no JiT nonce; errors={json.dumps(p1.get('errors'))}"}

    new_nonce = nonce_block["new_nonce"]
    nonce_ts = str(nonce_block.get("nonce_ts", ""))
    logger.info("V4W POST1 returned JiT nonce (ts=%s); retrying", nonce_ts)

    p2, r2 = _nf_post(form_data_json, security=new_nonce, nonce_ts=nonce_ts)

    if p2 is None:
        return {"success": False, "sub_id": None,
                "detail": f"POST2 non-JSON response (HTTP {r2.status_code})"}

    sub_id = _extract_sub_id(p2)
    if sub_id:
        logger.info("V4W submit success: sub_id=%s (POST2)", sub_id)
        return {"success": True, "sub_id": str(sub_id), "detail": "created on POST2"}

    errors = p2.get("errors")
    if errors and errors.get("nonce"):
        return {"success": False, "sub_id": None,
                "detail": "POST2 nonce-blocked (JiT loop) — form likely hardened"}
    return {"success": False, "sub_id": None,
            "detail": f"POST2 no sub_id; errors={json.dumps(errors)}"}


# ---------------------------------------------------------------------------
# Retry orchestration (Cloud Tasks) + referral logging
# ---------------------------------------------------------------------------

def enqueue_retry(payload: dict, attempt: int) -> bool:
    """Enqueue a Cloud Task to call /retry after RETRY_DELAY_SECONDS. payload is
    the full inbound /submit body (fields + uuid). attempt = the retry number the
    /retry invocation represents (1 = first retry). Returns True if enqueued."""
    if not TASKS_QUEUE:
        logger.error("TASKS_QUEUE unset — cannot enqueue retry (attempt=%s). "
                     "403 will NOT be retried for uuid=%s.",
                     attempt, payload.get("uuid"))
        return False

    client = tasks_v2.CloudTasksClient()
    parent = client.queue_path(TASKS_PROJECT, TASKS_LOCATION, TASKS_QUEUE)

    task_body = dict(payload)
    task_body["attempt"] = attempt

    schedule_time = (
        datetime.datetime.now(tz=datetime.timezone.utc)
        + datetime.timedelta(seconds=RETRY_DELAY_SECONDS)
    )
    ts = timestamp_pb2.Timestamp()
    ts.FromDatetime(schedule_time)

    task = {
        "http_request": {
            "http_method": tasks_v2.HttpMethod.POST,
            "url": f"{SERVICE_URL}/retry",
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(task_body).encode(),
        },
        "schedule_time": ts,
    }
    client.create_task(request={"parent": parent, "task": task})
    logger.info("Enqueued retry attempt=%s for uuid=%s in %ss",
                attempt, payload.get("uuid"), RETRY_DELAY_SECONDS)
    return True


def log_referral_row(uuid_value: str, retry_status: str) -> bool:
    """Write a Referrals row via sheet-service /write. Matches the flow's write
    node (uuid / timestamp / provider) plus Retry. Timestamp = write time
    (retry-success or final-fail moment) — the decided source of truth."""
    now_str = datetime.datetime.now().strftime("%Y-%m-%d, %H:%M")
    body = {
        "password": PASSWORD,
        "sheetid": SHEET_ID,
        "tab": SHEET_TAB,
        "newrow": "yes",
        "key": "uuid",
        "uuid": uuid_value,
        "timestamp": now_str,
        "provider": "Vets4Warriors",
        "Retry": retry_status,
    }
    try:
        r = requests.post(SHEET_WRITE_URL, json=body, timeout=SHEET_TIMEOUT)
        ok = r.status_code == 200
        if not ok:
            logger.error("sheet /write failed: HTTP %s | uuid=%s | retry=%s | body=%s",
                         r.status_code, uuid_value, retry_status, r.text[:300])
        else:
            logger.info("Logged Referrals row: uuid=%s Retry=%s", uuid_value, retry_status)
        return ok
    except requests.RequestException:
        logger.exception("sheet /write transport error: uuid=%s retry=%s",
                         uuid_value, retry_status)
        return False


# ---------------------------------------------------------------------------
# Field normalization
# ---------------------------------------------------------------------------

GENDER_DEFAULT = "Prefer not to Disclose"
BRANCH_DEFAULT = "Unknown"
DUTY_DEFAULT = "Did not obtain"

_BRANCH_MAP = {
    "army": "Army",
    "navy": "Navy",
    "air force": "Air Force",
    "coast guard": "Coast Guard",
    "marine corps": "USMC",
    "marines": "USMC",
    "usmc": "USMC",
    "army national guard": "Army National Guard",
    "air national guard": "Air National Guard",
    "space force": "Space Force",
}

_GENDER_MAP = {
    "male": "Male",
    "female": "Female",
    "non-binary": "Non-binary",
    "nonbinary": "Non-binary",
    "transgender female": "Transgender female",
    "transgender male": "Transgender male",
    "genderqueer": "Genderqueer",
    "questioning": "Not sure what their gender identity is (Questioning)",
    "other": "Other",
}


def _norm_key(s: str) -> str:
    return " ".join(s.replace("_", " ").lower().split())


def map_branch(raw: str) -> str:
    if not raw:
        return BRANCH_DEFAULT
    return _BRANCH_MAP.get(_norm_key(raw), BRANCH_DEFAULT)


def map_gender(raw: str) -> str:
    if not raw or "," in raw:
        return GENDER_DEFAULT
    return _GENDER_MAP.get(_norm_key(raw), GENDER_DEFAULT)


def normalize_fields(body: dict) -> dict:
    fname = (body.get("fname") or "").strip()
    lname = (body.get("lname") or "").strip()
    if not fname and body.get("name"):
        parts = str(body["name"]).strip().split(" ", 1)
        fname = parts[0]
        lname = parts[1] if len(parts) > 1 else ""

    raw_phone = str(body.get("phone", ""))
    digits = "".join(ch for ch in raw_phone if ch.isdigit())
    phone = digits[-10:] if len(digits) >= 10 else digits

    zipc = str(body.get("zip", "")).strip()

    gender = map_gender(str(body.get("gender", "")).strip())
    branch = map_branch(str(body.get("branch", body.get("militarybranch", ""))).strip())

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

    Body adds "uuid" (contact uuid) so a retry-recovered or final-fail Referrals
    row can be logged by this service (the flow logs the immediate-success row).
    On a Cloudflare 403: success=false, detail contains "403", AND a delayed
    retry is enqueued (the flow routes 403 -> info email -> ends)."""
    body, err = parse_body_or_400()
    if err:
        return err

    if not check_password(body):
        return jsonify({"status": "error", "message": "Unauthorized"}), 403

    fields_values = normalize_fields(body)

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

    if not result["success"] and is_403_detail(result["detail"]):
        logger.warning("V4W 403 on /submit for uuid=%s — enqueuing retry",
                       body.get("uuid"))
        # Isolate the enqueue from the response path: the TextIt response below
        # MUST return clean (success=false + 403 detail) regardless of whether
        # the Cloud Tasks enqueue succeeds. A failure here means only "retry not
        # queued" (logged) — never a 500 back to the flow (which would trip the
        # flow's transport-retry loop and cause a DUPLICATE V4W post).
        try:
            enqueue_retry(body, attempt=1)
        except Exception:
            logger.exception("enqueue_retry failed for uuid=%s — retry NOT queued; "
                             "returning clean 403 response to caller", body.get("uuid"))

    if not result["success"]:
        logger.error("V4W submission failed: %s | fields=%s",
                     result["detail"],
                     {k: fields_values[k] for k in ("fname", "lname", "zip")})

    return jsonify({
        "status": "success" if result["success"] else "error",
        "success": result["success"],
        "sub_id": result["sub_id"],
        "detail": result["detail"],
    }), 200


@app.route("/retry", methods=["POST"])
def retry():
    """Cloud-Tasks-invoked delayed retry of a 403-blocked submission.

    Body = original /submit body + "attempt" (1-based). SUCCESS -> log
    Retry="SUCCESS". 403 again with attempts left -> enqueue next. 403 with no
    attempts left, or any non-403 failure -> log Retry="FAIL". Returns 200 on
    handled outcomes so the queue does not add its own retry on top."""
    body, err = parse_body_or_400()
    if err:
        return err

    if not check_password(body):
        return jsonify({"status": "error", "message": "Unauthorized"}), 403

    attempt = int(body.get("attempt", 1))
    uuid_value = body.get("uuid", "")

    fields_values = normalize_fields(body)
    missing = [k for k in ("fname", "phone", "zip") if not fields_values.get(k)]
    if missing:
        logger.error("retry missing fields %s for uuid=%s — logging FAIL",
                     missing, uuid_value)
        log_referral_row(uuid_value, "FAIL")
        return jsonify({"status": "error", "message": "missing fields"}), 200

    try:
        result = submit_to_v4w(fields_values)
    except Exception:
        logger.exception("retry attempt=%s errored for uuid=%s", attempt, uuid_value)
        result = {"success": False, "sub_id": None, "detail": "retry transport error"}

    if result["success"]:
        logger.info("V4W retry attempt=%s SUCCEEDED for uuid=%s (sub_id=%s)",
                    attempt, uuid_value, result["sub_id"])
        log_referral_row(uuid_value, "SUCCESS")
        return jsonify({"status": "success", "success": True,
                        "sub_id": result["sub_id"], "attempt": attempt}), 200

    if is_403_detail(result["detail"]) and attempt + 1 < MAX_ATTEMPTS:
        logger.warning("V4W retry attempt=%s 403 for uuid=%s — enqueuing next",
                       attempt, uuid_value)
        # If the re-enqueue succeeds, return "retrying" and let the next task
        # resolve it. If it FAILS, do not silently drop the referral — fall
        # through to logging Retry=FAIL so the failure is at least visible in
        # the sheet (better a FAIL row than a vanished referral).
        try:
            if enqueue_retry(body, attempt=attempt + 1):
                return jsonify({"status": "retrying", "success": False,
                                "attempt": attempt}), 200
            logger.error("re-enqueue returned False (no queue?) uuid=%s — logging FAIL",
                         uuid_value)
        except Exception:
            logger.exception("re-enqueue threw uuid=%s — logging FAIL", uuid_value)

    logger.error("V4W retry FINAL FAIL attempt=%s uuid=%s detail=%s",
                 attempt, uuid_value, result["detail"])
    log_referral_row(uuid_value, "FAIL")
    return jsonify({"status": "error", "success": False,
                    "detail": result["detail"], "attempt": attempt}), 200


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

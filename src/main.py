import os
import re
import json
import time
import logging
import datetime

import requests
from flask import Flask, request, jsonify
from google.cloud import tasks_v2
from google.protobuf import timestamp_pb2
from google.cloud import firestore
from google.api_core.exceptions import AlreadyExists

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


# ---------------------------------------------------------------------------
# Idempotency (Firestore) — prevent duplicate V4W records on flow retries
# ---------------------------------------------------------------------------
# Root cause (2026-08-25): the V4W handshake sometimes takes longer than TextIt's
# ~15s webhook timeout. When it does, TextIt records a "Connection Error", takes
# the flow's Failure exit, and re-fires /submit via the resourceretrycount loop —
# but the FIRST (slow) call still succeeded server-side and created a V4W record.
# The retry then runs its own handshake and creates a SECOND record. Two sub_ids,
# ~8s apart, one veteran. Confirmed: Kevin Morin 814101+814102, Daniel melby
# 814107+814108. TextIt's 15s ceiling is not raisable from our side, so the fix is
# service-side idempotency keyed by contact uuid.
#
# Design (per the 2026-08-25 design discussion — this is the load-bearing mechanic):
#   - On /submit entry, atomically CLAIM the uuid in Firestore (create-if-absent).
#   - Claim WON  -> this call is first; run the handshake; on completion write the
#     outcome (success+sub_id, or failure) into the claim doc and return it.
#   - Claim LOST -> a submission for this uuid is already in flight (this call is a
#     retry). DO NOT post to V4W. Poll the doc for up to POLL_MAX_SECONDS:
#       * outcome becomes SUCCESS -> return that real sub_id (no second post).
#       * outcome becomes FAILURE -> return failure (flow may retry; a retry will
#         then find no live claim and be free to genuinely resubmit).
#       * still unresolved at poll end -> return FAILURE so TextIt KEEPS retrying.
#         Every subsequent retry also finds the live claim and polls — none post to
#         V4W — so churn is safe: it never duplicates and never fakes success.
#   - STALE claim (older than CLAIM_STALE_SECONDS with no outcome) -> the first
#     process is presumed dead; a retry may take over the claim and resubmit.
#     Known small residual: if the dead process created a V4W record just before
#     dying, the takeover duplicates. Rare; accepted.
#
# NEVER return a success-shaped response for an UNKNOWN outcome — doing so would
# stop TextIt's retries and silently drop a submission that later failed. Duplicate
# prevention comes from the CLAIM blocking the post, not from halting retries.
#
# If Firestore is unavailable / IDEMPOTENCY_ENABLED is false, the layer degrades
# OPEN: /submit behaves exactly as before (posts every time). Degrading open keeps
# the service working (no veteran blocked) at the cost of the old duplicate risk —
# the correct failure direction for a safety layer that must not itself drop posts.

IDEMPOTENCY_ENABLED = os.environ.get("IDEMPOTENCY_ENABLED", "true").lower() == "true"
FS_COLLECTION = os.environ.get("FS_COLLECTION", "v4w_submissions")
FS_PROJECT = os.environ.get("TASKS_PROJECT", "early-alert-responses")
# The project's (default) database is DATASTORE_MODE (us-east4, App Engine, 2022) —
# incompatible with the Native-mode client below. We target a SEPARATE named
# Native-mode database (us-east1). Generically named ("early-alert") so it can be
# the project's shared small-state DB — other services use their own COLLECTIONS
# in it; v4w's idempotency docs live under FS_COLLECTION. Set via env var.
FS_DATABASE = os.environ.get("FS_DATABASE", "early-alert")
CLAIM_STALE_SECONDS = int(os.environ.get("CLAIM_STALE_SECONDS", "90"))
POLL_MAX_SECONDS = int(os.environ.get("POLL_MAX_SECONDS", "12"))
POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "2"))

_fs_client = None


def _fs():
    """Lazily create the Firestore client targeting the named Native-mode database
    (NOT the (default) Datastore-mode DB). Guarded on None by callers."""
    global _fs_client
    if _fs_client is None:
        _fs_client = firestore.Client(project=FS_PROJECT, database=FS_DATABASE)
    return _fs_client


def _now_utc():
    return datetime.datetime.now(tz=datetime.timezone.utc)


def claim_or_status(uuid_value: str):
    """Atomically try to claim `uuid_value` for submission.

    Returns one of:
      ("claimed", None)                 -> caller is first; proceed to submit.
      ("in_progress", None)             -> a live claim exists; caller should poll.
      ("done", {"success":..., "sub_id":..., "detail":...})
                                        -> a terminal outcome already exists;
                                           caller returns it verbatim (no post).
      ("open", None)                    -> idempotency disabled/unavailable; caller
                                           proceeds WITHOUT dedupe (degrade open).

    Claim doc shape:
      { state: "in_progress"|"done", claimed_at: <ts>, outcome: {...}|null }
    """
    if not IDEMPOTENCY_ENABLED or not uuid_value:
        return "open", None
    try:
        doc_ref = _fs().collection(FS_COLLECTION).document(uuid_value)
        try:
            # Atomic create-if-absent: raises AlreadyExists if a claim is present.
            doc_ref.create({
                "state": "in_progress",
                "claimed_at": _now_utc(),
                "outcome": None,
            })
            return "claimed", None
        except AlreadyExists:
            snap = doc_ref.get()
            data = snap.to_dict() or {}
            if data.get("state") == "done" and data.get("outcome"):
                return "done", data["outcome"]
            # in_progress — check staleness
            claimed_at = data.get("claimed_at")
            if claimed_at is not None:
                # Firestore returns tz-aware datetimes
                age = (_now_utc() - claimed_at).total_seconds()
                if age > CLAIM_STALE_SECONDS:
                    # Presumed-dead first process: take over the claim.
                    doc_ref.set({
                        "state": "in_progress",
                        "claimed_at": _now_utc(),
                        "outcome": None,
                    })
                    logger.warning("Stale claim (%.0fs) for uuid=%s — taking over",
                                   age, uuid_value)
                    return "claimed", None
            return "in_progress", None
    except Exception:
        # Firestore error: degrade OPEN (submit proceeds). Never block a veteran
        # because the idempotency store hiccuped.
        logger.exception("Firestore claim error for uuid=%s — degrading open",
                         uuid_value)
        return "open", None


def record_outcome(uuid_value: str, result: dict) -> None:
    """Write the terminal outcome onto the claim doc so a polling retry can read
    it. Best-effort: a failure here does not change the response to the caller."""
    if not IDEMPOTENCY_ENABLED or not uuid_value:
        return
    try:
        _fs().collection(FS_COLLECTION).document(uuid_value).set({
            "state": "done",
            "outcome": {
                "success": bool(result.get("success")),
                "sub_id": result.get("sub_id"),
                "detail": result.get("detail"),
            },
            "resolved_at": _now_utc(),
        }, merge=True)
    except Exception:
        logger.exception("Firestore record_outcome error for uuid=%s", uuid_value)


def clear_claim(uuid_value: str) -> None:
    """Delete a claim so a FUTURE genuine resubmission of the same uuid (e.g. weeks
    later, or a real retry after a failure) is not blocked. Called after writing a
    FAILURE outcome, so failed attempts don't leave a lingering 'done=failure' doc
    that would suppress a legitimate later retry. Best-effort."""
    if not IDEMPOTENCY_ENABLED or not uuid_value:
        return
    try:
        _fs().collection(FS_COLLECTION).document(uuid_value).delete()
    except Exception:
        logger.exception("Firestore clear_claim error for uuid=%s", uuid_value)


def poll_for_outcome(uuid_value: str):
    """Poll the claim doc up to POLL_MAX_SECONDS for a terminal outcome.
    Returns the outcome dict if one lands, else None (still unresolved)."""
    if not IDEMPOTENCY_ENABLED or not uuid_value:
        return None
    waited = 0
    while waited < POLL_MAX_SECONDS:
        time.sleep(POLL_INTERVAL_SECONDS)
        waited += POLL_INTERVAL_SECONDS
        try:
            snap = _fs().collection(FS_COLLECTION).document(uuid_value).get()
            data = snap.to_dict() or {}
            if data.get("state") == "done" and data.get("outcome"):
                return data["outcome"]
        except Exception:
            logger.exception("Firestore poll error for uuid=%s", uuid_value)
            return None
    return None


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

    # --- Idempotency: claim the uuid before posting to V4W (dup prevention) ---
    uuid_value = body.get("uuid", "")
    claim_state, claim_outcome = claim_or_status(uuid_value)

    if claim_state == "done":
        # A prior call for this uuid already reached a terminal outcome. Return it
        # verbatim — no second V4W post.
        logger.info("Idempotency: uuid=%s already resolved (sub_id=%s) — returning "
                    "prior outcome, no re-post", uuid_value, claim_outcome.get("sub_id"))
        return jsonify({
            "status": "success" if claim_outcome.get("success") else "error",
            "success": claim_outcome.get("success"),
            "sub_id": claim_outcome.get("sub_id"),
            "detail": claim_outcome.get("detail"),
        }), 200

    if claim_state == "in_progress":
        # This call is a retry of an in-flight submission. DO NOT post. Poll for the
        # first call's outcome.
        logger.info("Idempotency: uuid=%s in progress — polling, not re-posting",
                    uuid_value)
        outcome = poll_for_outcome(uuid_value)
        if outcome is not None:
            # First call resolved while we polled — return its REAL outcome.
            return jsonify({
                "status": "success" if outcome.get("success") else "error",
                "success": outcome.get("success"),
                "sub_id": outcome.get("sub_id"),
                "detail": outcome.get("detail"),
            }), 200
        # Still unresolved. Return FAILURE so TextIt keeps retrying (each retry will
        # re-poll the live claim and still not post). NEVER fake success here.
        logger.info("Idempotency: uuid=%s still in progress after poll — returning "
                    "failure so caller retries (no post made)", uuid_value)
        return jsonify({
            "status": "error", "success": False, "sub_id": None,
            "detail": "submission in progress (idempotency hold) — retry",
        }), 200

    # claim_state is "claimed" (we're first) or "open" (idempotency off/unavailable):
    # proceed to the actual V4W submission.
    try:
        result = submit_to_v4w(fields_values)
    except requests.RequestException as e:
        logger.exception("Transport error submitting to V4W")
        # Transport failure = no record created. Clear the claim so a retry can
        # genuinely resubmit (do not leave a live claim that would block it).
        clear_claim(uuid_value)
        return jsonify({"status": "error", "success": False,
                        "message": f"V4W transport error: {e}"}), 502
    except Exception as e:
        logger.exception("Unexpected error submitting to V4W")
        clear_claim(uuid_value)
        return jsonify({"status": "error", "success": False,
                        "message": f"internal error: {e}"}), 500

    # Record the outcome onto the claim so a polling retry can read it.
    if result["success"]:
        record_outcome(uuid_value, result)
    else:
        # Failure: record then clear, so the flow's next retry finds no live claim
        # and is free to genuinely resubmit (a failed post created no record).
        record_outcome(uuid_value, result)
        clear_claim(uuid_value)

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

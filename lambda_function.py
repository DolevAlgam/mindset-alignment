import json
import os
import re
import time
import uuid
import urllib.request
from datetime import datetime, timedelta, timezone

import boto3

# Config
SLACK_WEBHOOK_URL = os.environ["SLACK_WEBHOOK_URL"]
RETELL_API_KEY = os.environ.get("RETELL_API_KEY", "")
S3_BUCKET = os.environ.get("S3_BUCKET", "")
FROM_NUMBER = os.environ.get("FROM_NUMBER", "")
TO_NUMBER = os.environ.get("TO_NUMBER", "")
AGENT_ID = os.environ.get("AGENT_ID", "")
TIMEZONE = os.environ.get("TIMEZONE", "America/New_York")
TRIGGER_API_KEY = os.environ.get("TRIGGER_API_KEY", "")
FALLBACK_HOUR = int(os.environ.get("FALLBACK_HOUR", "12"))
CUTOFF_HOUR = int(os.environ.get("CUTOFF_HOUR", "20"))
SCHEDULER_ROLE_ARN = os.environ.get("SCHEDULER_ROLE_ARN", "")
LAMBDA_ARN = os.environ["LAMBDA_ARN"]
SCHEDULE_GROUP = "retell-calls"
MAX_ATTEMPTS = 5
STATE_KEY = "state.json"
STALE_CALL_MINUTES = 10

s3_client = boto3.client("s3", region_name="us-east-1")
scheduler_client = boto3.client("scheduler", region_name="us-east-1")


# ---------------------------------------------------------------------------
# Timezone helpers
# ---------------------------------------------------------------------------

def _tz_offset():
    """Return a fixed UTC offset for the configured timezone.

    Python 3.9+ has zoneinfo, but we keep a simple ET mapping as fallback.
    """
    try:
        import zoneinfo
        return datetime.now(zoneinfo.ZoneInfo(TIMEZONE)).utcoffset()
    except Exception:
        # Rough fallback for America/New_York (EDT = -4, EST = -5)
        now_utc = datetime.now(timezone.utc)
        # EDT: second Sunday of March to first Sunday of November
        year = now_utc.year
        mar = datetime(year, 3, 8, 2, tzinfo=timezone.utc)
        while mar.weekday() != 6:
            mar += timedelta(days=1)
        nov = datetime(year, 11, 1, 2, tzinfo=timezone.utc)
        while nov.weekday() != 6:
            nov += timedelta(days=1)
        if mar <= now_utc < nov:
            return timedelta(hours=-4)
        return timedelta(hours=-5)


def _tz_abbreviation():
    """Return a speech-friendly timezone name (e.g. 'Eastern time', 'Pacific time')."""
    SPEECH_NAMES = {
        "EST": "Eastern time", "EDT": "Eastern time",
        "CST": "Central time", "CDT": "Central time",
        "MST": "Mountain time", "MDT": "Mountain time",
        "PST": "Pacific time", "PDT": "Pacific time",
        "IST": "Israel time", "IDT": "Israel time",
    }
    try:
        import zoneinfo
        abbr = datetime.now(zoneinfo.ZoneInfo(TIMEZONE)).strftime("%Z")
        return SPEECH_NAMES.get(abbr, abbr)
    except Exception:
        return TIMEZONE


def now_in_tz():
    offset = _tz_offset()
    return datetime.now(timezone(offset))


def today_str():
    return now_in_tz().strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# S3 State
# ---------------------------------------------------------------------------

DEFAULT_STATE = {
    "date": None,
    "status": "pending",
    "scheduled_call_time": None,
    "schedule_name": None,
    "call_attempts": 0,
    "last_attempt_time": None,
    "last_call_id": None,
    "triggers": [],
    "call_later_time": None,
    "completed_at": None,
    "today_data": None,
    "yesterday_data": None,
}


def get_state():
    try:
        resp = s3_client.get_object(Bucket=S3_BUCKET, Key=STATE_KEY)
        state = json.loads(resp["Body"].read().decode("utf-8"))
        # Merge with defaults for any missing keys
        for k, v in DEFAULT_STATE.items():
            if k not in state:
                state[k] = v
        return state
    except s3_client.exceptions.NoSuchKey:
        return dict(DEFAULT_STATE)
    except Exception:
        return dict(DEFAULT_STATE)


def save_state(state):
    s3_client.put_object(
        Bucket=S3_BUCKET,
        Key=STATE_KEY,
        Body=json.dumps(state, indent=2).encode("utf-8"),
        ContentType="application/json",
    )


def ensure_today(state):
    """Day rollover: if state date != today, reset and carry yesterday data."""
    td = today_str()
    if state.get("date") != td:
        yesterday_data = state.get("today_data")
        for k, v in DEFAULT_STATE.items():
            state[k] = v
        state["date"] = td
        state["yesterday_data"] = yesterday_data
    return state


# ---------------------------------------------------------------------------
# Retell API
# ---------------------------------------------------------------------------

def _format_previous_answers(today_data):
    """Format today_data into readable text for the agent prompt."""
    if not today_data:
        return "none"
    fields = [
        ("Vision", today_data.get("vision")),
        ("Most important goal", today_data.get("most_important_goal_now")),
        ("Yesterday performance", today_data.get("yesterday_performance_score")),
        ("Plan for today", today_data.get("daily_plan_for_goal")),
    ]
    parts = []
    has_any = False
    for label, value in fields:
        if value is not None and value != "":
            parts.append(f"- {label}: {value}")
            has_any = True
        else:
            parts.append(f"- {label}: (not answered yet)")
    return "\n".join(parts) if has_any else "none"


def retell_api(method, path, body=None):
    url = f"https://api.retellai.com{path}"
    data = json.dumps(body).encode("utf-8") if body else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {RETELL_API_KEY}",
            "Content-Type": "application/json",
        },
        method=method,
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode("utf-8"))


def initiate_call(state):
    now = now_in_tz()
    yesterday_plan = "none"
    if state.get("yesterday_data") and state["yesterday_data"].get("daily_plan_for_goal"):
        yesterday_plan = state["yesterday_data"]["daily_plan_for_goal"]

    resp = retell_api("POST", "/v2/create-phone-call", {
        "from_number": FROM_NUMBER,
        "to_number": TO_NUMBER,
        "override_agent_id": AGENT_ID,
        "retell_llm_dynamic_variables": {
            "yesterday_daily_plan": yesterday_plan,
            "current_time": now.strftime("%-I:%M %p ") + _tz_abbreviation(),
            "current_date": now.strftime("%A, %B %-d, %Y"),
            "timezone_name": _tz_abbreviation(),
            "previous_answers": _format_previous_answers(state.get("today_data")),
        },
    })

    state["status"] = "calling"
    state["call_attempts"] = state.get("call_attempts", 0) + 1
    state["last_call_id"] = resp.get("call_id")
    state["last_attempt_time"] = now.isoformat()
    state["schedule_name"] = None
    state["scheduled_call_time"] = None
    print(f"Initiated call: {resp.get('call_id')}, attempt #{state['call_attempts']}")
    return resp


# ---------------------------------------------------------------------------
# EventBridge Scheduler
# ---------------------------------------------------------------------------

def schedule_call(at_time_str, reason="scheduled"):
    """Create a one-time EventBridge schedule. at_time_str is ISO format in local tz."""
    name = f"retell-call-{today_str()}-{uuid.uuid4().hex[:8]}"
    # Format for EventBridge: at(yyyy-mm-ddThh:mm:ss)
    # Parse and reformat to ensure correct format
    dt = datetime.fromisoformat(at_time_str)
    sched_expr = f"at({dt.strftime('%Y-%m-%dT%H:%M:%S')})"

    scheduler_client.create_schedule(
        Name=name,
        GroupName=SCHEDULE_GROUP,
        ScheduleExpression=sched_expr,
        ScheduleExpressionTimezone=TIMEZONE,
        Target={
            "Arn": LAMBDA_ARN,
            "RoleArn": SCHEDULER_ROLE_ARN,
            "Input": json.dumps({"action": "initiate_call", "reason": reason}),
        },
        FlexibleTimeWindow={"Mode": "OFF"},
        ActionAfterCompletion="DELETE",
    )
    print(f"Scheduled call '{name}' at {sched_expr} ({reason})")
    return name, at_time_str


def cancel_schedule(name):
    if not name:
        return
    try:
        scheduler_client.delete_schedule(Name=name, GroupName=SCHEDULE_GROUP)
        print(f"Cancelled schedule: {name}")
    except Exception:
        pass  # Already deleted or doesn't exist


# ---------------------------------------------------------------------------
# Slack / CloudWatch helpers (preserved from original)
# ---------------------------------------------------------------------------

def parse_performance_score(value):
    """Extract integer 1-5 from values like '3', '3/5', 3, or '3 out of 5'.
    Returns None for 0, None, empty, or unparseable values (0 means no answer given)."""
    if not value:
        return None
    if isinstance(value, (int, float)):
        score = int(value)
        return score if 1 <= score <= 5 else None
    match = re.search(r"(\d)", str(value))
    if match:
        score = int(match.group(1))
        return score if 1 <= score <= 5 else None
    return None


def post_to_slack(text):
    payload = json.dumps({"text": text}).encode("utf-8")
    req = urllib.request.Request(
        SLACK_WEBHOOK_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    urllib.request.urlopen(req)


def emit_structured_log(vision, goal, score, plan, status="completed"):
    print(json.dumps({
        "log_type": "call_analysis",
        "status": status,
        "vision": vision,
        "most_important_goal_now": goal,
        "yesterday_performance_score": score,
        "daily_plan_for_goal": plan,
    }))


def emit_emf_metric(score):
    if score is not None:
        print(json.dumps({
            "_aws": {
                "Timestamp": int(time.time() * 1000),
                "CloudWatchMetrics": [{
                    "Namespace": "RetellSlackBridge",
                    "Dimensions": [["FunctionName"]],
                    "Metrics": [{"Name": "DailyPerformance", "Unit": "None"}],
                }],
            },
            "FunctionName": "retell-slack-bridge",
            "DailyPerformance": score,
        }))


def post_analysis_to_slack(vision, goal, performance, plan, prefix=""):
    fields = [
        ("Vision", vision),
        ("Most Important Goal Now", goal),
        ("Yesterday Performance", performance),
        ("Plan For Today", plan),
    ]
    text = "\n\n".join(f"*{label}:* {value}" for label, value in fields if value is not None)
    if prefix:
        text = f"{prefix}\n\n{text}"
    if text:
        post_to_slack(text)


# ---------------------------------------------------------------------------
# Stale call detection
# ---------------------------------------------------------------------------

def is_stale_call(state):
    """Return True if status is 'calling' but last attempt was >10 min ago."""
    if state.get("status") != "calling":
        return False
    last = state.get("last_attempt_time")
    if not last:
        return True
    last_dt = datetime.fromisoformat(last)
    if last_dt.tzinfo is None:
        last_dt = last_dt.replace(tzinfo=now_in_tz().tzinfo)
    return (now_in_tz() - last_dt).total_seconds() > STALE_CALL_MINUTES * 60


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def handle_trigger(body):
    state = get_state()
    state = ensure_today(state)

    if state["status"] == "completed":
        save_state(state)
        return {"statusCode": 200, "body": json.dumps({"message": "Already completed today"})}

    # If stale call, treat as failed
    if is_stale_call(state):
        state["status"] = "pending"

    source = body.get("source", "unknown")
    call_at = body.get("call_at")
    state["triggers"].append({"source": source, "call_at": call_at, "received_at": now_in_tz().isoformat()})

    # Determine earliest requested time
    now = now_in_tz()
    candidate_times = []

    if call_at:
        candidate_times.append(call_at)

    # Check other triggers for their call_at
    for t in state.get("triggers", []):
        if t.get("call_at"):
            candidate_times.append(t["call_at"])

    # Current scheduled time
    if state.get("scheduled_call_time"):
        candidate_times.append(state["scheduled_call_time"])

    # call_later_time override
    if state.get("call_later_time"):
        clt = state["call_later_time"]  # HH:MM
        clt_dt = datetime.strptime(f"{today_str()} {clt}", "%Y-%m-%d %H:%M")
        clt_iso = clt_dt.isoformat()
        candidate_times.append(clt_iso)

    if candidate_times:
        # Parse all to find earliest
        parsed = []
        for t in candidate_times:
            try:
                dt = datetime.fromisoformat(t)
                parsed.append((dt, t))
            except (ValueError, TypeError):
                continue
        if parsed:
            parsed.sort(key=lambda x: x[0])
            earliest_dt, earliest_iso = parsed[0]
        else:
            earliest_iso = (now + timedelta(minutes=1)).isoformat()
    else:
        # No call_at specified — call in 1 minute
        earliest_iso = (now + timedelta(minutes=1)).isoformat()

    # Schedule new call (old schedules are kept — they auto-skip if call already made)
    name, scheduled_time = schedule_call(earliest_iso, reason=f"trigger:{source}")
    state["status"] = "scheduled"
    state["scheduled_call_time"] = scheduled_time
    state["schedule_name"] = name

    save_state(state)
    return {"statusCode": 200, "body": json.dumps({"message": "Scheduled", "schedule_name": name, "call_at": scheduled_time})}


def handle_initiate_call(reason=""):
    state = get_state()
    state = ensure_today(state)
    is_call_later = reason.startswith("call_later")

    if state["status"] == "completed":
        save_state(state)
        print("Skipping initiate_call: already completed today")
        return {"statusCode": 200, "body": json.dumps({"message": "Already completed"})}

    if state["status"] == "calling" and not is_stale_call(state):
        save_state(state)
        print("Skipping initiate_call: call already in progress")
        return {"statusCode": 200, "body": json.dumps({"message": "Call in progress"})}

    # Retries skip when a future call_later is pending (call_later takes priority)
    if not is_call_later and state.get("call_later_time"):
        try:
            clt_dt = datetime.strptime(f"{today_str()} {state['call_later_time']}", "%Y-%m-%d %H:%M")
            clt_dt = clt_dt.replace(tzinfo=now_in_tz().tzinfo)
            if clt_dt > now_in_tz():
                save_state(state)
                print(f"Skipping retry: call_later pending at {state['call_later_time']}")
                return {"statusCode": 200, "body": json.dumps({"message": f"Skipped, call_later at {state['call_later_time']}"})}
        except ValueError:
            pass

    # call_later bypasses MAX_ATTEMPTS (user explicitly asked to be called back)
    if not is_call_later and state["call_attempts"] >= MAX_ATTEMPTS:
        save_state(state)
        print(f"Skipping initiate_call: max attempts ({MAX_ATTEMPTS}) reached")
        return {"statusCode": 200, "body": json.dumps({"message": "Max attempts reached"})}

    now = now_in_tz()
    if now.hour >= CUTOFF_HOUR:
        print("Past cutoff hour, running cutoff log instead")
        return handle_cutoff_log()

    initiate_call(state)
    if is_call_later:
        state["call_later_time"] = None
    save_state(state)
    return {"statusCode": 200, "body": json.dumps({"message": "Call initiated"})}


def handle_daily_fallback():
    state = get_state()
    state = ensure_today(state)

    if state["status"] == "completed":
        save_state(state)
        print("Daily fallback: already completed")
        return {"statusCode": 200, "body": json.dumps({"message": "Already completed"})}

    # If scheduled for the future, skip
    if state["status"] == "scheduled" and state.get("scheduled_call_time"):
        try:
            sched_dt = datetime.fromisoformat(state["scheduled_call_time"])
            if sched_dt.tzinfo is None:
                sched_dt = sched_dt.replace(tzinfo=now_in_tz().tzinfo)
            if sched_dt > now_in_tz():
                save_state(state)
                print(f"Daily fallback: call already scheduled for {state['scheduled_call_time']}")
                return {"statusCode": 200, "body": json.dumps({"message": "Already scheduled"})}
        except (ValueError, TypeError):
            pass

    # If currently calling and not stale, skip
    if state["status"] == "calling" and not is_stale_call(state):
        save_state(state)
        print("Daily fallback: call in progress")
        return {"statusCode": 200, "body": json.dumps({"message": "Call in progress"})}

    # Stale call — treat as failed
    if is_stale_call(state):
        state["status"] = "pending"

    if state["call_attempts"] >= MAX_ATTEMPTS:
        save_state(state)
        print("Daily fallback: max attempts reached")
        return {"statusCode": 200, "body": json.dumps({"message": "Max attempts reached"})}

    initiate_call(state)
    save_state(state)
    return {"statusCode": 200, "body": json.dumps({"message": "Fallback call initiated"})}


def handle_call_ended(body):
    state = get_state()
    call = body.get("call", {})
    call_status = call.get("call_status", "")
    disconnection_reason = call.get("disconnection_reason", "")
    direction = call.get("direction", "outbound")
    print(f"call_ended: status={call_status}, reason={disconnection_reason}, direction={direction}")

    no_answer_reasons = {
        "dial_no_answer", "dial_busy", "voicemail_reached", "machine_detected",
    }

    if call_status == "not_connected" or disconnection_reason in no_answer_reasons:
        # No answer — retry in 1 hour if allowed
        now = now_in_tz()
        if state["call_attempts"] < MAX_ATTEMPTS and now.hour < CUTOFF_HOUR:
            retry_time = (now + timedelta(hours=1)).isoformat()
            name, scheduled_time = schedule_call(retry_time, reason=f"retry:no_answer:{disconnection_reason}")
            state["status"] = "scheduled"
            state["scheduled_call_time"] = scheduled_time
            state["schedule_name"] = name
            print(f"Retry scheduled for {scheduled_time}")
        else:
            print("No more retries available (max attempts or past cutoff)")
    elif call_status == "ended":
        if disconnection_reason in ("user_hangup", "agent_hangup", "inactivity"):
            # Wait for call_analyzed webhook
            print(f"Call ended ({disconnection_reason}), waiting for call_analyzed")
        elif disconnection_reason.startswith("error"):
            # Error — retry
            now = now_in_tz()
            if direction == "inbound" and now.hour < FALLBACK_HOUR:
                print("Inbound error before fallback — ignoring, fallback will handle")
            elif state["call_attempts"] < MAX_ATTEMPTS and now.hour < CUTOFF_HOUR:
                retry_time = (now + timedelta(hours=1)).isoformat()
                name, scheduled_time = schedule_call(retry_time, reason=f"retry:error:{disconnection_reason}")
                state["status"] = "scheduled"
                state["scheduled_call_time"] = scheduled_time
                state["schedule_name"] = name
            else:
                print("Error but no retries available")

    save_state(state)
    return {"statusCode": 200, "body": json.dumps({"message": "call_ended processed"})}


def handle_call_analyzed(body):
    state = get_state()
    state = ensure_today(state)

    call = body.get("call", {})
    direction = call.get("direction", "outbound")
    analysis = call.get("call_analysis", {})
    custom = analysis.get("custom_analysis_data", {})

    # Discard voicemail data — Retell flags voicemails via in_voicemail but
    # still runs post-call analysis which extracts garbage from the greeting.
    # Since call_ended is not in webhook_events, retries must be scheduled here.
    if analysis.get("in_voicemail"):
        print("call_analyzed: voicemail detected (in_voicemail=true), discarding")
        now = now_in_tz()
        if state["call_attempts"] < MAX_ATTEMPTS and now.hour < CUTOFF_HOUR:
            retry_time = (now + timedelta(hours=1)).isoformat()
            name, scheduled_time = schedule_call(retry_time, reason="retry:voicemail")
            state["status"] = "scheduled"
            state["scheduled_call_time"] = scheduled_time
            state["schedule_name"] = name
            print(f"Voicemail retry scheduled for {scheduled_time}")
        save_state(state)
        return {"statusCode": 200, "body": json.dumps({"message": "Voicemail ignored, retry scheduled"})}

    vision = custom.get("vision")
    goal = custom.get("most_important_goal_now")
    yesterday_performance = custom.get("yesterday_performance")
    plan = custom.get("daily_plan_for_goal")
    call_later_time = custom.get("call_later_time")
    performance_score = parse_performance_score(yesterday_performance)

    print(f"call_analyzed: vision={vision}, goal={goal}, perf={yesterday_performance}, plan={plan}, call_later={call_later_time}, direction={direction}")

    # "Call me later" — schedule callback and notify Slack
    if call_later_time and call_later_time.strip():
        try:
            clt_dt = datetime.strptime(f"{today_str()} {call_later_time.strip()}", "%Y-%m-%d %H:%M")
            clt_with_tz = clt_dt.replace(tzinfo=now_in_tz().tzinfo)
            if clt_with_tz > now_in_tz():
                clt_iso = clt_dt.isoformat()
                name, scheduled_time = schedule_call(clt_iso, reason=f"call_later:{call_later_time}")
                state["status"] = "scheduled"
                state["scheduled_call_time"] = scheduled_time
                state["schedule_name"] = name
                state["call_later_time"] = call_later_time.strip()
                save_state(state)
                print(f"Call later scheduled for {call_later_time}")
                post_to_slack(f"Next call scheduled for {call_later_time} {_tz_abbreviation()} (call me later)")
                return {"statusCode": 200, "body": json.dumps({"message": "Call later scheduled"})}
            else:
                print(f"call_later_time {call_later_time} is in the past, treating as partial call")
        except ValueError:
            print(f"Could not parse call_later_time: {call_later_time}")

    # Check if all 4 fields are present and non-empty (complete call)
    all_present = all(v for v in [vision, goal, yesterday_performance, plan])

    if all_present:
        state["status"] = "completed"
        state["completed_at"] = now_in_tz().isoformat()
        state["today_data"] = {
            "vision": vision,
            "most_important_goal_now": goal,
            "yesterday_performance_score": performance_score,
            "daily_plan_for_goal": plan,
        }

        emit_structured_log(vision, goal, performance_score, plan)
        emit_emf_metric(performance_score)
        post_analysis_to_slack(vision, goal, yesterday_performance, plan)
        save_state(state)
        return {"statusCode": 200, "body": json.dumps({"message": "Forwarded to Slack"})}

    # Partial call — store what we have and retry
    partial_data = {}
    if vision:
        partial_data["vision"] = vision
    if goal:
        partial_data["most_important_goal_now"] = goal
    if performance_score is not None:
        partial_data["yesterday_performance_score"] = performance_score
    if plan:
        partial_data["daily_plan_for_goal"] = plan

    # Merge with existing partial data
    if state.get("today_data"):
        for k, v in partial_data.items():
            state["today_data"][k] = v
    elif partial_data:
        state["today_data"] = partial_data

    # Inbound zero-data (no fields at all): before fallback, ignore and let fallback handle
    now = now_in_tz()
    if direction == "inbound" and not partial_data:
        if now.hour < FALLBACK_HOUR:
            print("Inbound zero-data before fallback — ignoring, fallback will handle")
            save_state(state)
            return {"statusCode": 200, "body": json.dumps({"message": "Inbound ignored pre-fallback"})}
        # After fallback: fall through to regular retry logic below

    if state["call_attempts"] < MAX_ATTEMPTS and now.hour < CUTOFF_HOUR:
        retry_time = (now + timedelta(hours=1)).isoformat()
        name, scheduled_time = schedule_call(retry_time, reason="retry:partial_call")
        state["status"] = "scheduled"
        state["scheduled_call_time"] = scheduled_time
        state["schedule_name"] = name
        print("Partial call — retry scheduled")
    else:
        # Can't retry — log partial data
        state["status"] = "completed"
        state["completed_at"] = now_in_tz().isoformat()
        td = state.get("today_data") or {}
        emit_structured_log(
            td.get("vision"), td.get("most_important_goal_now"),
            td.get("yesterday_performance_score"), td.get("daily_plan_for_goal"),
            status="partial",
        )
        emit_emf_metric(td.get("yesterday_performance_score"))
        post_analysis_to_slack(
            td.get("vision"), td.get("most_important_goal_now"),
            yesterday_performance, td.get("daily_plan_for_goal"),
            prefix="[Partial]",
        )
        print("Partial call — no more retries, logged partial data")

    save_state(state)
    return {"statusCode": 200, "body": json.dumps({"message": "call_analyzed processed"})}


def handle_cutoff_log():
    state = get_state()
    state = ensure_today(state)

    if state["status"] == "completed":
        save_state(state)
        print("Cutoff: already completed")
        return {"statusCode": 200, "body": json.dumps({"message": "Already completed"})}

    td = state.get("today_data")
    if td:
        vision = td.get("vision")
        goal = td.get("most_important_goal_now")
        score = td.get("yesterday_performance_score")
        plan = td.get("daily_plan_for_goal")

        emit_structured_log(vision, goal, score, plan, status="partial")
        emit_emf_metric(score)
        post_analysis_to_slack(vision, goal, str(score) if score else None, plan, prefix="[Partial]")
    else:
        print(json.dumps({"log_type": "call_analysis", "status": "no_call_completed"}))
        post_to_slack("No morning call completed today.")

    state["status"] = "completed"
    state["completed_at"] = now_in_tz().isoformat()
    save_state(state)
    return {"statusCode": 200, "body": json.dumps({"message": "Cutoff logged"})}


# ---------------------------------------------------------------------------
# Main handler
# ---------------------------------------------------------------------------

def lambda_handler(event, context):
    print(f"Event: {json.dumps(event)}")

    # EventBridge Scheduler invocation (raw JSON, no HTTP wrapper)
    if "action" in event:
        action = event["action"]
        if action == "initiate_call":
            return handle_initiate_call(reason=event.get("reason", ""))
        if action == "daily_fallback":
            return handle_daily_fallback()
        if action == "cutoff_log":
            return handle_cutoff_log()

    # HTTP event (function URL)
    body = json.loads(event.get("body", "{}"))

    # Trigger API
    if body.get("action") == "trigger":
        headers = event.get("headers", {})
        api_key = headers.get("x-api-key", "")
        if api_key != TRIGGER_API_KEY:
            return {"statusCode": 401, "body": json.dumps({"error": "Unauthorized"})}
        return handle_trigger(body)

    # Retell webhooks
    retell_event = body.get("event")
    print(f"Retell event: {retell_event}")

    if retell_event == "call_ended":
        return handle_call_ended(body)
    if retell_event == "call_analyzed":
        return handle_call_analyzed(body)

    return {"statusCode": 200, "body": json.dumps({"message": f"Ignored: {retell_event}"})}

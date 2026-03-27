# Retell Slack Bridge

## What This Is

A single AWS Lambda that orchestrates daily morning calls via Retell AI:
1. **Triggers calls** via external triggers (alarm, location, manual) or daily fallback
2. **Manages call lifecycle**: scheduling, retries (no-answer, partial, errors), "call me later"
3. **Posts results** to Slack via webhook
4. **Logs structured data** to CloudWatch Logs (auto-queryable via Logs Insights)
5. **Emits CloudWatch metrics** for daily performance score via Embedded Metric Format (EMF)

## Architecture

```
TRIGGER SOURCES                         AWS                                  RETELL AI
───────────────                         ───                                  ─────────
Alarm OFF ──┐
Location ───┼─ POST /trigger ─► Lambda ──► S3 state ──► EventBridge Scheduler
Manual API ─┘   (API key auth)              │               │
                                            │               ▼ (at scheduled time)
EventBridge Daily ──────────────────────────►│          Lambda ──► Retell API
(12 PM fallback)                            │               │    create-phone-call
                                            │               │    + dynamic vars
EventBridge 8 PM ───────────────────────────►│               │
(cutoff: log partial)                       │               ▼
                                            │          Phone Call
                                            │               │
                                            │     Retell webhooks:
                                            │     call_ended ──► Lambda ──► retry?
                                            │     call_analyzed ──► Lambda ──► Slack
                                            │                              ──► CloudWatch
                                            │                              ──► S3 (store data)
```

## AWS Resources

| Resource | Name |
|----------|------|
| Lambda Function | `retell-slack-bridge` |
| Lambda IAM Role | `retell-slack-bridge-role` |
| Lambda Log Group | `/aws/lambda/retell-slack-bridge` |
| S3 Bucket | `retell-call-state-{account_id}` |
| EventBridge Schedule Group | `retell-calls` |
| EventBridge Schedule | `daily-fallback` (cron, configurable hour) |
| EventBridge Schedule | `cutoff-log` (cron, configurable hour) |
| Scheduler IAM Role | `retell-scheduler-role` |
| CloudWatch Dashboard | [`RetellSlackBridge`](https://us-east-1.console.aws.amazon.com/cloudwatch/home?region=us-east-1#dashboards/dashboard/RetellSlackBridge) |
| CloudWatch Metric | `RetellSlackBridge / DailyPerformance` |

## Environment Variables (Lambda)

| Variable | Description |
|----------|-------------|
| `SLACK_WEBHOOK_URL` | Slack incoming webhook URL for posting call summaries |
| `RETELL_API_KEY` | Retell AI API key (Bearer auth) |
| `S3_BUCKET` | S3 bucket for daily state |
| `FROM_NUMBER` | Retell/Twilio phone number to call from |
| `TO_NUMBER` | Phone number to call |
| `AGENT_ID` | Retell agent ID |
| `TIMEZONE` | IANA timezone for scheduling (e.g. `America/New_York`) |
| `TRIGGER_API_KEY` | API key for trigger endpoint authentication |
| `FALLBACK_HOUR` | Hour (24h) for daily fallback call (default: `12`) |
| `CUTOFF_HOUR` | Hour (24h) for end-of-day cutoff (default: `20`) |
| `SCHEDULER_ROLE_ARN` | IAM role ARN for EventBridge Scheduler |
| `LAMBDA_ARN` | Lambda function ARN |

## Files

| File | Purpose |
|------|---------|
| `lambda_function.py` | Lambda handler: event routing, state management, call orchestration, Slack/CW output |
| `provision_scheduler.py` | Infra provisioning: S3, IAM, EventBridge, Lambda config. Reads `.env` |
| `update_agent.py` | Sets Retell agent prompt, post-call analysis variables, and s2s_model. Reads `.env` |
| `create_dashboard.py` | CloudWatch dashboard creation/update |
| `.env` | Local secrets (gitignored): `RETELL_API_KEY`, `AGENT_ID`, `FROM_NUMBER`, `TO_NUMBER` |
| `.env.example` | Template for `.env` |
| `CLAUDE.md` | This file |
| `README.md` | Retell agent configuration, prompt, and post-call analysis setup |

## Deployment

Run all 4 steps in order for a full deploy:

```bash
# 1. Provision infra (S3, IAM, EventBridge schedules, Lambda env vars + timeout)
source .env && python provision_scheduler.py

# 2. Deploy Lambda code
zip function.zip lambda_function.py && aws lambda update-function-code \
  --function-name retell-slack-bridge --zip-file fileb://function.zip --region us-east-1

# 3. Update Retell agent (prompt + post-call analysis variables)
source .env && python update_agent.py

# 4. Update CloudWatch dashboard
python create_dashboard.py
```

Steps 1 and 3 require `.env` to be sourced (see `.env.example`).

All scripts are safe to re-run:
- `provision_scheduler.py` creates resources if missing, updates if existing. `RETELL_API_KEY` and `TRIGGER_API_KEY` are only set on Lambda if not already present (won't overwrite).
- `update_agent.py` always sets the full prompt and analysis variables to the desired state.
- `create_dashboard.py` always overwrites the dashboard with the desired state.
- Lambda deploy always overwrites the function code.

## S3 State (`state.json`)

Single JSON file tracks daily call lifecycle:
```json
{
  "date": "2026-03-06",
  "status": "pending|scheduled|calling|completed",
  "scheduled_call_time": null,
  "schedule_name": null,
  "call_attempts": 0,
  "last_attempt_time": null,
  "last_call_id": null,
  "triggers": [],
  "call_later_time": null,
  "completed_at": null,
  "today_data": null,
  "yesterday_data": null
}
```

**Day rollover**: When any invocation sees `date != today`, state resets and `today_data` copies to `yesterday_data`.

## Event Routing

The Lambda routes based on event shape:
- **EventBridge Scheduler**: `{"action": "initiate_call|daily_fallback|cutoff_log", "reason": "..."}` (reason tracks why: `call_later:17:00`, `retry:no_answer:voicemail_reached`, `trigger:alarm`, etc.)
- **HTTP trigger API**: `{"action": "trigger", "source": "...", "call_at": "..."}` with `x-api-key` header
- **Retell webhooks**: `{"event": "call_ended|call_analyzed", "call": {...}}`

## Trigger API

```bash
# Call in 4 hours (from alarm)
curl -X POST $FUNCTION_URL -H "Content-Type: application/json" -H "x-api-key: $TRIGGER_API_KEY" \
  -d '{"action":"trigger","source":"alarm","call_at":"2026-03-06T14:00:00"}'

# Call now (manual)
curl -X POST $FUNCTION_URL -H "Content-Type: application/json" -H "x-api-key: $TRIGGER_API_KEY" \
  -d '{"action":"trigger","source":"manual"}'
```

## Key Design Decisions

- **boto3 in Lambda**: Used for S3 state and EventBridge Scheduler (available in Lambda runtime by default)
- **Structured logging via `print()`**: Lambda auto-captures stdout to CloudWatch Logs
- **EMF metrics**: Performance score emitted via Embedded Metric Format (no extra IAM needed)
- **EventBridge Scheduler**: One-time schedules with `ActionAfterCompletion=DELETE` for call scheduling
- **Stale call detection**: If status is "calling" but >10 min since last attempt, treat as failed
- **Max 8 retries per day**, 1 hour between retries. **call_later bypasses this limit** — user-requested callbacks always fire regardless of attempt count
- **Cutoff hour**: Logs whatever data exists (partial or none)
- **Yesterday context**: Injected via `retell_llm_dynamic_variables` at call creation
- **Previous answers context**: Partial answers from earlier calls injected via `previous_answers` dynamic variable so retry calls skip already-answered questions
- **Inbound call handling**: Uses `call["direction"]` from Retell webhooks. Inbound zero-data before fallback hour → ignored (fallback handles it). Inbound zero-data after fallback → regular retry. Inbound partial data → always retry. Inbound call_later → always honored.
- **Schedules are never canceled**: New schedules are created alongside old ones. Old schedules fire and are no-ops (status checks skip redundant calls). Auto-deleted by EventBridge after firing. **Retries skip when a future call_later is pending** (call_later takes priority over automatic retries).
- **call_later_time validation**: Must be in the future; past times are ignored and fall through to normal retry logic
- **Slack notifications**: call_later posts the next scheduled call time to Slack for visibility
- **Speech-friendly timezone**: `timezone_name` dynamic variable uses natural names ("Eastern time") instead of abbreviations ("EDT")
- **Voicemail guard**: `call_analyzed` checks Retell's `in_voicemail` flag and discards data — Retell still runs post-call analysis on voicemails, extracting garbage from the greeting
- **Confirmation recap**: Agent repeats back all answers before the pump-up sentence to catch transcription errors (mandatory in prompt)

## Retell Webhook Payload Structure

**call_analyzed** (with `call_later_time` field):
```json
{
  "event": "call_analyzed",
  "call": {
    "direction": "inbound|outbound",
    "call_analysis": {
      "custom_analysis_data": {
        "vision": "...",
        "most_important_goal_now": "...",
        "yesterday_performance": "3/5",
        "daily_plan_for_goal": "...",
        "call_later_time": "15:00"
      }
    }
  }
}
```

**call_ended**:
```json
{
  "event": "call_ended",
  "call": {
    "call_id": "...",
    "call_status": "ended|not_connected|error",
    "disconnection_reason": "user_hangup|agent_hangup|dial_no_answer|...",
    "direction": "inbound|outbound"
  }
}
```

## Working With This Repo

- The Lambda uses boto3 (available in Lambda runtime) plus Python stdlib
- `function.zip` is the deployment artifact (just `lambda_function.py` zipped), gitignored
- AWS CLI and boto3 are needed locally for provisioning/deployment scripts
- Copy `.env.example` to `.env` and fill in your secrets before running provisioning scripts

# Test Cases

## Setup

```bash
# Set these for your environment
FURL="<your-lambda-function-url>"
TRIGGER_API_KEY="<your-trigger-api-key>"
S3_BUCKET="retell-call-state-<your-account-id>"

# Read S3 state
aws s3 cp s3://$S3_BUCKET/state.json - | python3 -m json.tool

# Write/seed S3 state
echo '{"date":"...","status":"..."}' | aws s3 cp - s3://$S3_BUCKET/state.json

# Invoke Lambda directly (simulates EventBridge)
aws lambda invoke --function-name retell-slack-bridge --region us-east-1 \
  --payload '{"action":"..."}' /dev/stdout

# List EventBridge schedules
aws scheduler list-schedules --group-name retell-calls --region us-east-1
```

---

## TC1: Happy Path — Trigger + Complete Call

**Type**: Automated setup + real phone call

```bash
# 1. Clear state for fresh day
echo '{}' | aws s3 cp - s3://$S3_BUCKET/state.json

# 2. Send trigger to call in 1 minute
NOW_PLUS_1=$(python3 -c "from datetime import datetime,timedelta; import zoneinfo; print((datetime.now(zoneinfo.ZoneInfo('America/New_York'))+timedelta(minutes=1)).strftime('%Y-%m-%dT%H:%M:%S'))")
curl -s -X POST "$FURL" \
  -H "Content-Type: application/json" \
  -H "x-api-key: $TRIGGER_API_KEY" \
  -d "{\"action\":\"trigger\",\"source\":\"manual\",\"call_at\":\"$NOW_PLUS_1\"}"

# 3. Check state is scheduled
aws s3 cp s3://$S3_BUCKET/state.json - | python3 -m json.tool
# ASSERT: status=scheduled, scheduled_call_time set, call_attempts=0

# 4. Check EventBridge schedule exists
aws scheduler list-schedules --group-name retell-calls --region us-east-1
# ASSERT: one-time schedule present

# 5. USER ACTION: Answer the phone, answer all 4 questions normally

# 6. Wait ~2 min for call_analyzed webhook, then check state
aws s3 cp s3://$S3_BUCKET/state.json - | python3 -m json.tool
# ASSERT: status=completed, today_data has all 4 fields non-null

# 7. Check Slack channel for message with vision, goal, performance, plan
```

---

## TC2: No Answer → Retry

**Type**: Fully automated (simulate webhooks, no real call needed)

```bash
# 1. Seed state as if a call was just initiated
cat <<'EOF' | aws s3 cp - s3://$S3_BUCKET/state.json
{"date":"2026-03-07","status":"calling","scheduled_call_time":null,"schedule_name":null,"call_attempts":1,"last_attempt_time":"2026-03-07T12:00:00","last_call_id":"test-call-1","triggers":[],"call_later_time":null,"completed_at":null,"today_data":null,"yesterday_data":null}
EOF

# 2. Simulate Retell call_ended webhook (no answer)
curl -s -X POST "$FURL" \
  -H "Content-Type: application/json" \
  -d '{"event":"call_ended","call":{"call_id":"test-call-1","call_status":"not_connected","disconnection_reason":"dial_no_answer"}}'

# 3. Check state
aws s3 cp s3://$S3_BUCKET/state.json - | python3 -m json.tool
# ASSERT: status=scheduled, schedule_name set, call_attempts still 1

# 4. Check new retry schedule exists
aws scheduler list-schedules --group-name retell-calls --region us-east-1
# ASSERT: one-time schedule ~1 hour from now

# 5. Clean up: delete the one-time schedule
aws scheduler delete-schedule --name <schedule-name> --group-name retell-calls --region us-east-1
```

---

## TC3: Partial Call — User Hangs Up Early

**Type**: Fully automated (simulate webhooks)

```bash
# 1. Seed state as calling
cat <<'EOF' | aws s3 cp - s3://$S3_BUCKET/state.json
{"date":"2026-03-07","status":"calling","scheduled_call_time":null,"schedule_name":null,"call_attempts":1,"last_attempt_time":"2026-03-07T14:00:00","last_call_id":"test-call-2","triggers":[],"call_later_time":null,"completed_at":null,"today_data":null,"yesterday_data":null}
EOF

# 2. Simulate call_ended (user hangup) — Lambda should do nothing, wait for analysis
curl -s -X POST "$FURL" \
  -H "Content-Type: application/json" \
  -d '{"event":"call_ended","call":{"call_id":"test-call-2","call_status":"ended","disconnection_reason":"user_hangup"}}'

# 3. Simulate call_analyzed with only 2 of 4 fields
curl -s -X POST "$FURL" \
  -H "Content-Type: application/json" \
  -d '{"event":"call_analyzed","call":{"call_id":"test-call-2","call_analysis":{"custom_analysis_data":{"vision":"Build the future","most_important_goal_now":"Launch MVP","yesterday_performance":null,"daily_plan_for_goal":null,"call_later_time":""}}}}'

# 4. Check state
aws s3 cp s3://$S3_BUCKET/state.json - | python3 -m json.tool
# ASSERT: status=scheduled (retry), call_attempts=1, NOT completed
# ASSERT: No Slack message posted (partial, mid-day)

# 5. Check retry schedule exists
aws scheduler list-schedules --group-name retell-calls --region us-east-1
# ASSERT: one-time schedule ~1 hour from now

# 6. Clean up schedule
```

---

## TC4: 8 PM Cutoff With Partial Data

**Type**: Fully automated

```bash
# 1. Seed state as if partial call happened + retries failed
cat <<'EOF' | aws s3 cp - s3://$S3_BUCKET/state.json
{"date":"2026-03-07","status":"scheduled","scheduled_call_time":"2026-03-07T21:00:00","schedule_name":null,"call_attempts":4,"last_attempt_time":"2026-03-07T19:00:00","last_call_id":"test-call-3","triggers":[],"call_later_time":null,"completed_at":null,"today_data":{"vision":"Build the future","most_important_goal_now":"Launch MVP","yesterday_performance_score":3,"daily_plan_for_goal":null},"yesterday_data":null}
EOF

# 2. Invoke cutoff_log action (simulates 8 PM EventBridge)
aws lambda invoke --function-name retell-slack-bridge --region us-east-1 \
  --payload '{"action":"cutoff_log"}' /dev/stdout

# 3. Check state
aws s3 cp s3://$S3_BUCKET/state.json - | python3 -m json.tool
# ASSERT: status=completed

# 4. Check Slack for "[Partial]" message with 3 fields shown, plan=null or missing
# 5. Check CloudWatch Logs for structured log with log_type=call_analysis, status=partial
```

---

## TC5: Zero Data — 8 PM Cutoff

**Type**: Fully automated

```bash
# 1. Seed state as if all attempts were no-answer (max attempts reached, no data)
cat <<'EOF' | aws s3 cp - s3://$S3_BUCKET/state.json
{"date":"2026-03-07","status":"pending","scheduled_call_time":null,"schedule_name":null,"call_attempts":5,"last_attempt_time":"2026-03-07T16:00:00","last_call_id":null,"triggers":[],"call_later_time":null,"completed_at":null,"today_data":null,"yesterday_data":null}
EOF

# 2. Invoke cutoff_log
aws lambda invoke --function-name retell-slack-bridge --region us-east-1 \
  --payload '{"action":"cutoff_log"}' /dev/stdout

# 3. Check state
aws s3 cp s3://$S3_BUCKET/state.json - | python3 -m json.tool
# ASSERT: status=completed

# 4. Check Slack for "No morning call completed today" message
# 5. Check CloudWatch Logs for {"log_type":"call_analysis","status":"no_call_completed"}
# 6. ASSERT: No EMF metric emitted (no score)
```

---

## TC6: "Call Me Later"

**Type**: Fully automated (simulate webhook)

```bash
# 1. Seed state as calling
cat <<'EOF' | aws s3 cp - s3://$S3_BUCKET/state.json
{"date":"2026-03-07","status":"calling","scheduled_call_time":null,"schedule_name":null,"call_attempts":1,"last_attempt_time":"2026-03-07T10:00:00","last_call_id":"test-call-4","triggers":[],"call_later_time":null,"completed_at":null,"today_data":null,"yesterday_data":null}
EOF

# 2. Simulate call_analyzed with call_later_time set, no answers
curl -s -X POST "$FURL" \
  -H "Content-Type: application/json" \
  -d '{"event":"call_analyzed","call":{"call_id":"test-call-4","call_analysis":{"custom_analysis_data":{"vision":null,"most_important_goal_now":null,"yesterday_performance":null,"daily_plan_for_goal":null,"call_later_time":"15:00"}}}}'

# 3. Check state
aws s3 cp s3://$S3_BUCKET/state.json - | python3 -m json.tool
# ASSERT: status=scheduled, call_later_time="15:00", scheduled_call_time contains 15:00

# 4. ASSERT: No Slack message posted (deferred, not completed)

# 5. Check schedule exists for 3:00 PM
aws scheduler list-schedules --group-name retell-calls --region us-east-1
# ASSERT: one-time schedule at 15:00

# 6. Clean up schedule
```

---

## TC7: Multiple Triggers — Earliest Wins

**Type**: Fully automated

```bash
# 1. Clear state
echo '{}' | aws s3 cp - s3://$S3_BUCKET/state.json

# 2. First trigger: call at 15:00
curl -s -X POST "$FURL" \
  -H "Content-Type: application/json" \
  -H "x-api-key: $TRIGGER_API_KEY" \
  -d '{"action":"trigger","source":"alarm","call_at":"2026-03-07T15:00:00"}'

# 3. Check state
aws s3 cp s3://$S3_BUCKET/state.json - | python3 -m json.tool
# ASSERT: scheduled_call_time contains 15:00, 1 trigger in triggers[]

# 4. Note the schedule name
SCHED1=$(aws s3 cp s3://$S3_BUCKET/state.json - | python3 -c "import sys,json; print(json.load(sys.stdin).get('schedule_name',''))")

# 5. Second trigger: call at 13:00 (earlier — should win)
curl -s -X POST "$FURL" \
  -H "Content-Type: application/json" \
  -H "x-api-key: $TRIGGER_API_KEY" \
  -d '{"action":"trigger","source":"location","call_at":"2026-03-07T13:00:00"}'

# 6. Check state
aws s3 cp s3://$S3_BUCKET/state.json - | python3 -m json.tool
# ASSERT: scheduled_call_time now 13:00, 2 triggers in triggers[]
# ASSERT: schedule_name changed (old schedule deleted, new one created)

# 7. Verify old schedule is gone and new one exists
aws scheduler list-schedules --group-name retell-calls --region us-east-1
# ASSERT: only 1 one-time schedule, at 13:00

# 8. Clean up schedule
```

---

## TC8: Day Rollover — Yesterday's Data

**Type**: Fully automated

```bash
# 1. Seed state as yesterday's completed call
cat <<'EOF' | aws s3 cp - s3://$S3_BUCKET/state.json
{"date":"2026-03-06","status":"completed","scheduled_call_time":null,"schedule_name":null,"call_attempts":1,"last_attempt_time":"2026-03-06T10:00:00","last_call_id":"old-call","triggers":[],"call_later_time":null,"completed_at":"2026-03-06T10:05:00","today_data":{"vision":"Build the future of logistics","most_important_goal_now":"Launch MVP","yesterday_performance_score":4,"daily_plan_for_goal":"Ship the onboarding flow"},"yesterday_data":null}
EOF

# 2. Invoke daily_fallback (state date is yesterday → triggers rollover)
# NOTE: This will actually call Retell API and initiate a real call!
# If you DON'T want a real call, skip to step 3 alternative.
aws lambda invoke --function-name retell-slack-bridge --region us-east-1 \
  --payload '{"action":"daily_fallback"}' /dev/stdout

# 3. Check state
aws s3 cp s3://$S3_BUCKET/state.json - | python3 -m json.tool
# ASSERT: date=today
# ASSERT: yesterday_data.daily_plan_for_goal = "Ship the onboarding flow"
# ASSERT: today_data = null
# ASSERT: status=calling, call_attempts=1

# 3-alt. If you don't want a real call, instead send a trigger far in the future:
curl -s -X POST "$FURL" \
  -H "Content-Type: application/json" \
  -H "x-api-key: $TRIGGER_API_KEY" \
  -d '{"action":"trigger","source":"manual","call_at":"2026-03-07T23:59:00"}'
# Then check state — day rollover should happen, yesterday_data populated, no call yet
```

---

## TC9: Trigger After Completion — Rejected

**Type**: Fully automated

```bash
# 1. Seed state as completed today
cat <<'EOF' | aws s3 cp - s3://$S3_BUCKET/state.json
{"date":"2026-03-07","status":"completed","scheduled_call_time":null,"schedule_name":null,"call_attempts":1,"last_attempt_time":"2026-03-07T10:00:00","last_call_id":"done-call","triggers":[],"call_later_time":null,"completed_at":"2026-03-07T10:05:00","today_data":{"vision":"X","most_important_goal_now":"Y","yesterday_performance_score":4,"daily_plan_for_goal":"Z"},"yesterday_data":null}
EOF

# 2. Send a trigger
curl -s -X POST "$FURL" \
  -H "Content-Type: application/json" \
  -H "x-api-key: $TRIGGER_API_KEY" \
  -d '{"action":"trigger","source":"alarm","call_at":"2026-03-07T15:00:00"}'
# ASSERT: Response says "Already completed today"

# 3. Check state is unchanged
aws s3 cp s3://$S3_BUCKET/state.json - | python3 -m json.tool
# ASSERT: status still completed, no new schedule

# 4. Invoke daily_fallback — should also skip
aws lambda invoke --function-name retell-slack-bridge --region us-east-1 \
  --payload '{"action":"daily_fallback"}' /dev/stdout
# ASSERT: Response indicates skipped

# 5. Invoke cutoff_log — should also skip
aws lambda invoke --function-name retell-slack-bridge --region us-east-1 \
  --payload '{"action":"cutoff_log"}' /dev/stdout
# ASSERT: Response indicates skipped, no duplicate Slack message
```

---

## TC10: Real End-to-End — Trigger → Call → Answer All Questions

**Type**: Real phone call (user participates)

```bash
# 1. Clear state completely
echo '{}' | aws s3 cp - s3://$S3_BUCKET/state.json

# 2. Send manual trigger to call NOW (no call_at = immediate)
curl -s -X POST "$FURL" \
  -H "Content-Type: application/json" \
  -H "x-api-key: $TRIGGER_API_KEY" \
  -d '{"action":"trigger","source":"manual"}'

# 3. USER ACTION: Phone will ring within ~1 minute. Answer and:
#    - Answer all 4 questions (vision, goal, yesterday performance, today's plan)
#    - Agent should reference yesterday's plan if state had yesterday_data
#    - After answering all questions, agent gives motivational line and says bye

# 4. Wait ~2 minutes after call ends for call_analyzed webhook

# 5. Check final state
aws s3 cp s3://$S3_BUCKET/state.json - | python3 -m json.tool
# ASSERT: status=completed
# ASSERT: today_data has all 4 fields non-null
# ASSERT: call_attempts >= 1

# 6. Check Slack for the message
# 7. Check CloudWatch Logs Insights:
#    SOURCE '/aws/lambda/retell-slack-bridge' | filter log_type = 'call_analysis' | sort @timestamp desc | limit 1
# ASSERT: All 4 fields present in log

# 8. Verify no more calls happen by invoking daily_fallback and cutoff_log:
aws lambda invoke --function-name retell-slack-bridge --region us-east-1 \
  --payload '{"action":"daily_fallback"}' /dev/stdout
# ASSERT: Skipped (completed)
```

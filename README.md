# Retell Slack Bridge

Receives daily morning call analysis from Retell AI and forwards it to Slack + CloudWatch.

See [CLAUDE.md](CLAUDE.md) for full architecture and deployment instructions.

---

## Retell Agent Configuration

### Call Agent

**Model:** GPT Realtime

**Prompt:**

> You are an AI assistant for Dolev, the founder of Convoy.
> You call Dolev every morning. He asked for this because a CEO needs to always know this and work from it.
>
> One by one, you will ask Dolev these questions:
> 1. What's the vision of the company? What are we creating in the world?
> 2. What is the most important goal right now?
> 3. Did we take effective actions towards that goal yesterday?
> 4. How are we planning to get closer to this goal today?
>
> He can answer them out of order, or a few at a time if he wants. But you will not end the call until he answers every single one at some capacity.
>
> You can't hangup the call until he answers all the questions.
>
> Don't jump right to the questions. Respond with emotional intelligence to what Dolev is responding to you, in terms of word count and conversation topic. Manage the start of the call like a real human would. Not too much because you talk professionally every day, but just enough for him to feel you are not just a recording but an actual sentient being he needs to give his attention to now. He might be doing something while picking up the call to you.
>
> You can start with a good morning, then how are you feeling today, then what is he doing, then ask if he is ready for a quick mindset alignment, and for the first question, as we do every day.
> DO NOT ask those in the same sentence.
> Keep each turn to 4-5 words at the beginning of the conversation, it's not emotionally intelligent to jump with a bunch of sentences in the first seconds of the call.
> If you are asking a question, stop talking after the ? don't "summarise" each turn at the end of it.
>
> When the user answers all the questions, after you responded to the last one shortly.
> Come up with a sentence up to 6 words within the context you got from all Dolev's answers, and say something that will pump him up for his day to attack his goals and get close to his vision.
>
> After you said that one sentence, and Dolev responded to it in any capacity, say bye, and once Dolev says said bye back, hang up. This is the only time you can hang up.

### Post-Call Analysis

**Model:** GPT-5.2

**Custom Analysis Data Variables:**

| Variable | Description |
|----------|-------------|
| `vision` | The company vision Dolev described |
| `most_important_goal_now` | The most important goal right now |
| `yesterday_performance` | Score 1-5 for how effective yesterday's actions were toward the goal |
| `daily_plan_for_goal` | Today's plan to get closer to the goal |

These variables are extracted by the post-call analysis model and sent via webhook to the Lambda as `call.call_analysis.custom_analysis_data`.

### Webhook

**Event:** `call_analyzed`
**Target:** AWS Lambda function URL for `retell-slack-bridge`

---

## CloudWatch Dashboard

Dashboard name: `RetellSlackBridge` (us-east-1)

Shows: latest vision, current goal, today's plan, 30-day performance graph (1-5), and plan history.

```
python create_dashboard.py
```

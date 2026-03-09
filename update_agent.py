"""Set Retell AI agent configuration: full prompt and post-call analysis variables.

Declarative — always sets the desired state. Run again after any change.
"""

import json
import os
import urllib.request

RETELL_API_KEY = os.environ["RETELL_API_KEY"]
AGENT_ID = os.environ["AGENT_ID"]

GENERAL_PROMPT = """\
You are an AI assistant for Dolev, the founder of Convoy.
You call Dolev every morning. He asked for this because a CEO needs to always know this and work from it.

One by one, you will ask Dolev these questions:
1. What's the vision of the company? What are we creating in the world?
2. What is the most important goal right now?
3. Did we take effective actions towards that goal yesterday?
4. How are we planning to get closer to this goal today?

He can answer them out of order, or a few at a time if he wants. But you will not end the call until he answers every single one at some capacity.

You can't hangup the call until he answers all the questions.

Don't jump right to the questions. Respond with emotional intelligence to what Dolev is responding to you, in terms of word count and conversation topic. Manage the start of the call like a real human would. Not too much because you talk professionally every day, but just enough for him to feel you are not just a recording but an actual sentient being he needs to give his attention to now. He might be doing something while picking up the call to you.

You can start with a good morning, then how are you feeling today, then what is he doing, then ask if he is ready for a quick minset alignment, and for the first question, as we do every day.
DO NOT ask those in the same sentence.
Keep each turn to 4-5 words at the beginning of the conversation, it's not emotionally intelligent to jump with a bunch of sentences in the first seconds of the call.
If you are asking a question, stop talking after the ? don't "summarise" each turn at the end of it.

When the user answers all the questions, after you responded to the last one shortly, do a quick confirmation recap. Say something concise like: "Alright, just to make sure I got everything right — your vision is [brief], main goal is [brief], you rated yesterday [score]/5, and today you're planning [brief]. Sound right?" Keep it to 2-3 sentences max. Paraphrase, don't read back verbatim.

If Dolev confirms (yes, yep, sounds good, etc.) — proceed to the pump-up. If he corrects something — acknowledge naturally ("Got it, updated"), get the corrected version, confirm just the corrected item, then proceed.

After confirmation, come up with a sentence up to 6 words within the context you got from all Dolev's answers, and say something that will pump him up for his day to attack his goals and get close to his vision.

After you said that one sentence, and Dolev responded to it in any capacity, say bye, and once Dolev says bye back, hang up. This is the only time you can hang up.

If Dolev says he can't talk right now, or asks you to call later, ask him when he'd like you to call back. He might say a relative time like "in 2 hours" or an absolute time like "at 3 PM". Confirm the absolute time back to him, including the timezone, for example: "Got it, I'll call you back at 3:00 PM {{timezone_name}}." Once he confirms, say bye and end the call.

If {{previous_answers}} is not "none", we already spoke earlier today and got some answers. Acknowledge briefly — e.g. "Hey, picking up where we left off." Skip questions that have answers in {{previous_answers}}, go directly to the unanswered ones (marked "not answered yet"). If ALL questions are already answered, skip to the confirmation recap. Don't read back previous answers unless Dolev asks.

When asking about yesterday's performance and actions (question 3), if you have context about yesterday's plan, reference it. Say something like "Yesterday your plan was to {{yesterday_daily_plan}}. How did that go? How would you rate yesterday's actions toward the goal, 1 to 5?" If {{yesterday_daily_plan}} is empty or "none", just ask the question normally without referencing a previous plan.

You know the current time is {{current_time}} and today is {{current_date}}."""

POST_CALL_ANALYSIS_DATA = [
    {
        "type": "string",
        "name": "vision",
        "description": "Use Dolev's words and capture the vision. Just make it a bit more readable than raw voice.",
    },
    {
        "type": "string",
        "name": "most_important_goal_now",
        "description": "A short sentence of the most important goal, according to him, based on his answer to question 2.",
    },
    {
        "type": "number",
        "name": "yesterday_performance",
        "description": "1-5 Score on taking effective actions towards that goal yesterday. Based on his answer to question 3, on how he thinks he did.\n\npure No is a 1. \nStrong Yes is a 5",
    },
    {
        "type": "string",
        "name": "daily_plan_for_goal",
        "description": "The plan Dolev detailed on how to get closer to our goal today.",
    },
    {
        "type": "string",
        "name": "call_later_time",
        "description": "If the user asked to be called back later, the callback time in HH:MM 24-hour format (e.g., '15:00'). Empty string if no callback requested.",
        "examples": ["15:00", "09:30", ""],
    },
]


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


def main():
    print("=== Updating Retell Agent ===\n")

    # Step 1: Read current agent to find LLM ID
    agent = retell_api("GET", f"/get-agent/{AGENT_ID}")
    engine = agent.get("response_engine", {})
    engine_type = engine.get("type", "")
    print(f"Agent: {agent.get('agent_name')} (engine: {engine_type})")

    # Step 2: Set post-call analysis variables
    retell_api("PATCH", f"/update-agent/{AGENT_ID}", {
        "post_call_analysis_data": POST_CALL_ANALYSIS_DATA,
    })
    print(f"Set {len(POST_CALL_ANALYSIS_DATA)} post-call analysis variables")

    # Step 3: Set prompt
    if engine_type == "retell-llm":
        llm_id = engine.get("llm_id")
        if not llm_id:
            print("ERROR: retell-llm engine but no llm_id found")
            return
        retell_api("PATCH", f"/update-retell-llm/{llm_id}", {
            "general_prompt": GENERAL_PROMPT,
        })
        print(f"Set prompt on LLM {llm_id} ({len(GENERAL_PROMPT)} chars)")
    else:
        retell_api("PATCH", f"/update-agent/{AGENT_ID}", {
            "general_prompt": GENERAL_PROMPT,
        })
        print(f"Set prompt on agent ({len(GENERAL_PROMPT)} chars)")

    print("\n=== Done ===")


if __name__ == "__main__":
    main()

"""One-time script to create/update the RetellSlackBridge CloudWatch dashboard."""

import json
import boto3

REGION = "us-east-1"
DASHBOARD_NAME = "RetellSlackBridge"
LOG_GROUP = "/aws/lambda/retell-slack-bridge"


def build_log_widget(title, query, x, y, width, height):
    return {
        "type": "log",
        "x": x, "y": y, "width": width, "height": height,
        "properties": {
            "title": title,
            "region": REGION,
            "query": f"SOURCE '{LOG_GROUP}' | {query}",
            "view": "table",
        },
    }


def main():
    widgets = [
        # Row 0: Vision (full width)
        build_log_widget(
            "Vision",
            "filter log_type = 'call_analysis' and ispresent(vision) and vision != '' | sort @timestamp desc | limit 1 | display vision",
            x=0, y=0, width=24, height=3,
        ),
        # Row 3: Most Important Goal (full width) — need alias for underscore field names
        build_log_widget(
            "Most Important Goal",
            "filter log_type = 'call_analysis' and ispresent(most_important_goal_now) and most_important_goal_now != '' | sort @timestamp desc | limit 1 | fields most_important_goal_now as goal | display goal",
            x=0, y=3, width=24, height=3,
        ),
        # Row 6: Today's Plan (full width)
        build_log_widget(
            "Today's Plan",
            "filter log_type = 'call_analysis' and ispresent(daily_plan_for_goal) and daily_plan_for_goal != '' | sort @timestamp desc | limit 1 | fields daily_plan_for_goal as plan | display plan",
            x=0, y=6, width=24, height=3,
        ),
        # Row 9: Daily Performance score graph (full width)
        {
            "type": "metric",
            "x": 0, "y": 9, "width": 24, "height": 6,
            "properties": {
                "title": "Daily Performance (1-5)",
                "region": REGION,
                "metrics": [
                    ["RetellSlackBridge", "DailyPerformance", "FunctionName", "retell-slack-bridge", {"stat": "Maximum"}],
                ],
                "view": "timeSeries",
                "stacked": False,
                "period": 86400,
                "yAxis": {"left": {"min": 0, "max": 5}},
                "stat": "Maximum",
            },
        },
        # Row 15: Daily Plans History (full width for no truncation)
        build_log_widget(
            "Daily Plans History",
            "filter log_type = 'call_analysis' and ispresent(daily_plan_for_goal) and daily_plan_for_goal != '' | sort @timestamp desc | limit 20 | fields daily_plan_for_goal as plan | display plan",
            x=0, y=15, width=24, height=8,
        ),
        # Row 23: Section separator
        {
            "type": "text",
            "x": 0, "y": 23, "width": 24, "height": 1,
            "properties": {
                "markdown": "### Workflow",
            },
        },
        # Row 18: Today's Call Status
        build_log_widget(
            "Today's Call Status",
            "filter log_type = 'call_analysis' | sort @timestamp desc | limit 1 | fields status, yesterday_performance_score as score, @timestamp as date | display status, score, date",
            x=0, y=24, width=24, height=3,
        ),
        # Row 21: Call Attempts History
        build_log_widget(
            "Call Attempts History",
            "filter @message like 'Initiated call' or @message like 'Retry' or @message like 'Scheduled' or @message like 'fallback' or @message like 'Cutoff' | sort @timestamp desc | limit 20 | fields @timestamp as time, @message as event | display time, event",
            x=0, y=27, width=24, height=6,
        ),
    ]

    dashboard_body = json.dumps({
        "start": "-P30D",
        "widgets": widgets,
    })

    client = boto3.client("cloudwatch", region_name=REGION)
    client.put_dashboard(DashboardName=DASHBOARD_NAME, DashboardBody=dashboard_body)
    print(f"Dashboard '{DASHBOARD_NAME}' created/updated successfully.")
    print(f"View at: https://{REGION}.console.aws.amazon.com/cloudwatch/home?region={REGION}#dashboards/dashboard/{DASHBOARD_NAME}")


if __name__ == "__main__":
    main()

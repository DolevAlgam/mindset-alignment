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
        # Row 0: Call Status (today)
        build_log_widget(
            "Today's Call Status",
            "filter log_type = 'call_analysis' | sort @timestamp desc | limit 1 | fields @timestamp, status, vision, most_important_goal_now, yesterday_performance_score, daily_plan_for_goal",
            x=0, y=0, width=24, height=3,
        ),
        # Row 3: Vision (full width)
        build_log_widget(
            "Vision",
            "filter log_type = 'call_analysis' and status != 'no_call_completed' | sort @timestamp desc | limit 1 | fields vision",
            x=0, y=3, width=24, height=3,
        ),
        # Row 6: Most Important Goal (full width)
        build_log_widget(
            "Most Important Goal Now",
            "filter log_type = 'call_analysis' and status != 'no_call_completed' | sort @timestamp desc | limit 1 | fields most_important_goal_now",
            x=0, y=6, width=24, height=3,
        ),
        # Row 9: Today's Plan (full width)
        build_log_widget(
            "Today's Plan",
            "filter log_type = 'call_analysis' and status != 'no_call_completed' | sort @timestamp desc | limit 1 | fields daily_plan_for_goal",
            x=0, y=9, width=24, height=3,
        ),
        # Row 12 left: Daily Performance score graph
        {
            "type": "metric",
            "x": 0, "y": 12, "width": 12, "height": 6,
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
        # Row 12 right: Plans History
        build_log_widget(
            "Daily Plans History",
            "filter log_type = 'call_analysis' and status != 'no_call_completed' | sort @timestamp desc | limit 20 | fields @timestamp, daily_plan_for_goal",
            x=12, y=12, width=12, height=6,
        ),
        # Row 18: Call Attempts History
        build_log_widget(
            "Call Attempts History",
            "filter @message like 'Initiated call' or @message like 'Retry scheduled' or @message like 'Scheduled call' | sort @timestamp desc | limit 20 | fields @timestamp, @message",
            x=0, y=18, width=24, height=6,
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

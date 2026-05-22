"""One-off probe: does a Chart.Line render in a Teams card posted via the
incoming webhook? Run once, then check the Teams channel. Safe to delete."""

import os

import requests

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

url = os.environ.get("TEAMS_WEBHOOK_URL", "").strip()
if not url:
    raise SystemExit("ERROR: TEAMS_WEBHOOK_URL not set (.env)")

# Mock 'price' series — not real data, just to draw a line.
points = [
    ("May 15", 38.1), ("May 16", 41.7), ("May 17", 40.2), ("May 18", 44.9),
    ("May 19", 43.5), ("May 20", 48.8), ("May 21", 52.3),
]

chart = {
    "type": "Chart.Line",
    "title": "IONQ — mock price (chart test)",
    "xAxisTitle": "Day",
    "yAxisTitle": "Price ($)",
    "colorSet": "categorical",
    "data": [{
        "legend": "IONQ (mock)",
        "values": [{"x": d, "y": v} for d, v in points],
    }],
    "fallback": {
        "type": "TextBlock",
        "wrap": True,
        "color": "Attention",
        "weight": "Bolder",
        "text": ("FALLBACK — this client did not render the chart. "
                 "Data was: " + ", ".join(f"{d} ${v}" for d, v in points)),
    },
}

card = {
    "type": "AdaptiveCard",
    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
    "version": "1.5",
    "msteams": {"width": "Full"},
    "body": [
        {"type": "TextBlock", "text": "📊 Adaptive Card chart test",
         "weight": "Bolder", "size": "Large"},
        {"type": "TextBlock", "wrap": True, "isSubtle": True,
         "text": ("Below this line: a line chart = charts work via webhook; "
                  "a red FALLBACK message = chart unsupported; nothing at "
                  "all = the host ignored it entirely.")},
        chart,
    ],
}

payload = {"type": "message", "attachments": [{
    "contentType": "application/vnd.microsoft.card.adaptive",
    "content": card,
}]}

resp = requests.post(url, json=payload, timeout=20)
print("HTTP", resp.status_code, "|", resp.text[:200])
print("Posted. Now check the Teams channel for the 'chart test' card.")

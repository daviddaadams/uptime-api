#!/usr/bin/env python3
"""
Simple webhook receiver example for testing alerts.
Run this in a separate terminal to receive uptime notifications.

Usage:
    python webhook_example.py
    
Then create a monitor with webhook_url: http://localhost:5000/webhook
"""

from flask import Flask, request, jsonify
from datetime import datetime

app = Flask(__name__)

@app.route('/webhook', methods=['POST'])
def receive_webhook():
    data = request.json
    timestamp = datetime.fromtimestamp(data['timestamp'])
    
    print("\n" + "="*60)
    print(f"🚨 ALERT RECEIVED at {timestamp}")
    print("="*60)
    print(f"Monitor: {data['name']}")
    print(f"URL: {data['url']}")
    print(f"Status: {data['previous_status']} → {data['status']}")
    print(f"Status Code: {data.get('status_code', 'N/A')}")
    print(f"Response Time: {data.get('response_time_ms', 'N/A')}ms")
    print("="*60 + "\n")
    
    return jsonify({"received": True}), 200

if __name__ == '__main__':
    print("🎣 Webhook receiver listening on http://localhost:5000/webhook")
    print("Press Ctrl+C to stop\n")
    app.run(port=5000, debug=False)

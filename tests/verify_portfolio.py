import requests
import asyncio
import sys

BACKEND_URL = "http://localhost:8000"

def check_portfolio():
    print(f"Checking GET {BACKEND_URL}/health...")
    try:
        res = requests.get(f"{BACKEND_URL}/health")
        print(f"Health Status: {res.status_code}")
        print(res.json())
    except Exception as e:
        print(f"Health Check Failed: {e}")
        return

    print(f"\nChecking GET {BACKEND_URL}/portfolio...")
    try:
        res = requests.get(f"{BACKEND_URL}/portfolio")
        if res.status_code == 200:
            print("Portfolio Endpoint OK")
            print(f"Data: {res.json()}")
        else:
            print(f"Portfolio Endpoint Failed: {res.status_code}")
            print(res.text)
    except Exception as e:
        print(f"Portfolio Check Failed: {e}")

if __name__ == "__main__":
    check_portfolio()

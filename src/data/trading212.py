"""Trading 212 API client — read-only."""

import base64
import os
import time
import requests

BASE_URL_DEMO = "https://demo.trading212.com/api/v0"
BASE_URL_LIVE = "https://live.trading212.com/api/v0"


class Trading212Client:
    def __init__(self, use_live: bool = False):
        key = os.environ["T212_API_KEY"]
        secret = os.environ["T212_API_SECRET"]
        token = base64.b64encode(f"{key}:{secret}".encode()).decode()
        self.headers = {
            "Authorization": f"Basic {token}",
            "Content-Type": "application/json",
        }
        self.base_url = BASE_URL_LIVE if use_live else BASE_URL_DEMO

    def _get(self, path: str) -> dict:
        url = f"{self.base_url}{path}"
        response = requests.get(url, headers=self.headers, timeout=30)
        remaining = response.headers.get("x-ratelimit-remaining")
        if remaining is not None and int(remaining) < 5:
            time.sleep(2)
        response.raise_for_status()
        return response.json()

    def get_account_summary(self) -> dict:
        return self._get("/equity/account/summary")

    def get_cash(self) -> dict:
        return self._get("/equity/account/cash")

    def get_positions(self) -> list:
        return self._get("/equity/portfolio/positions")

    def get_order_history(self) -> list:
        return self._get("/equity/history/orders")

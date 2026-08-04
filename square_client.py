"""
Read-only client for the Square API.

IMPORTANT: This module intentionally exposes ONLY read/search endpoints
(GET requests, and Square's POST-based "search" endpoints, which are
read operations that do not create/modify/delete any data in your Square
account). Do not add create/update/delete calls here -- this app is
designed to never write to your Square account.
"""
import time

import requests

import config


class SquareAPIError(Exception):
    """Raised for non-retryable Square API errors (auth, bad request, etc.)."""

    def __init__(self, message, status_code=None, category=None, code=None):
        super().__init__(message)
        self.status_code = status_code
        self.category = category
        self.code = code


class SquareAuthError(SquareAPIError):
    """Raised when the access token is missing, invalid, or expired."""


MAX_RETRIES = 5
INITIAL_BACKOFF_SECONDS = 1.0


class SquareClient:
    def __init__(self, access_token=None, base_url=None, api_version=None):
        self.access_token = access_token or config.SQUARE_ACCESS_TOKEN
        self.base_url = base_url or config.SQUARE_API_BASE
        self.api_version = api_version or config.SQUARE_VERSION
        if not self.access_token:
            raise SquareAuthError(
                "No Square access token configured. Set SQUARE_ACCESS_TOKEN in your .env file."
            )

    def _headers(self):
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Square-Version": self.api_version,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _request(self, method, path, params=None, json_body=None):
        """Perform a single read-only HTTP request with retry/backoff handling."""
        if method.upper() not in ("GET", "POST"):
            # POST is only ever used here for Square's read-only /search endpoints.
            raise ValueError("SquareClient only supports read operations (GET/search POST).")

        url = f"{self.base_url}{path}"
        backoff = INITIAL_BACKOFF_SECONDS
        last_error = None

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = requests.request(
                    method,
                    url,
                    headers=self._headers(),
                    params=params,
                    json=json_body,
                    timeout=30,
                )
            except requests.RequestException as exc:
                last_error = exc
                if attempt == MAX_RETRIES:
                    raise SquareAPIError(f"Network error contacting Square API: {exc}") from exc
                time.sleep(backoff)
                backoff *= 2
                continue

            if resp.status_code == 200:
                return resp.json()

            if resp.status_code in (401, 403):
                detail = _extract_error_detail(resp)
                raise SquareAuthError(
                    "Square API rejected the request as unauthorized. Your access token may be "
                    f"missing, invalid, expired, or lack the required scopes. Details: {detail}",
                    status_code=resp.status_code,
                )

            if resp.status_code == 429 or resp.status_code >= 500:
                # Rate limited or transient server error -- retry with backoff.
                last_error = _extract_error_detail(resp)
                if attempt == MAX_RETRIES:
                    raise SquareAPIError(
                        f"Square API request failed after {MAX_RETRIES} attempts "
                        f"(HTTP {resp.status_code}): {last_error}",
                        status_code=resp.status_code,
                    )
                retry_after = resp.headers.get("Retry-After")
                sleep_for = float(retry_after) if retry_after else backoff
                time.sleep(sleep_for)
                backoff *= 2
                continue

            # Other 4xx errors -- not retryable.
            detail = _extract_error_detail(resp)
            raise SquareAPIError(
                f"Square API request failed (HTTP {resp.status_code}): {detail}",
                status_code=resp.status_code,
            )

        raise SquareAPIError(f"Square API request failed after retries: {last_error}")

    # ------------------------------------------------------------------
    # Locations
    # ------------------------------------------------------------------
    def list_locations(self):
        data = self._request("GET", "/v2/locations")
        return data.get("locations", [])

    # ------------------------------------------------------------------
    # Catalog (service menu / pricing)
    # ------------------------------------------------------------------
    def search_catalog_objects(self, object_types=("ITEM", "ITEM_VARIATION", "CATEGORY")):
        """Yields catalog objects of the given types, handling pagination."""
        cursor = None
        while True:
            body = {"object_types": list(object_types), "include_deleted_objects": False}
            if cursor:
                body["cursor"] = cursor
            data = self._request("POST", "/v2/catalog/search", json_body=body)
            for obj in data.get("objects", []):
                yield obj
            cursor = data.get("cursor")
            if not cursor:
                break

    # ------------------------------------------------------------------
    # Orders (line-item level revenue)
    # ------------------------------------------------------------------
    def search_orders(self, location_ids, begin_time=None, end_time=None, states=("OPEN", "COMPLETED", "CANCELED")):
        """Yields orders for the given locations, handling pagination.

        Defaults to every Order state (not just COMPLETED): some checkout flows
        leave an order's state lagging behind or never advancing to COMPLETED
        even after payment succeeds, and filtering server-side to COMPLETED-only
        would silently drop those orders before they ever reach the local cache.
        Which orders count as a "real" sale is decided downstream from payment
        status instead of order state -- see analysis.py.
        """
        cursor = None
        date_filter = {}
        if begin_time or end_time:
            created_at = {}
            if begin_time:
                created_at["start_at"] = begin_time
            if end_time:
                created_at["end_at"] = end_time
            date_filter["date_time_filter"] = {"created_at": created_at}

        while True:
            body = {
                "location_ids": list(location_ids),
                "query": {
                    "filter": {
                        "state_filter": {"states": list(states)},
                        **date_filter,
                    },
                    "sort": {"sort_field": "CREATED_AT", "sort_order": "ASC"},
                },
                "limit": 500,
            }
            if cursor:
                body["cursor"] = cursor
            data = self._request("POST", "/v2/orders/search", json_body=body)
            for order in data.get("orders", []):
                yield order
            cursor = data.get("cursor")
            if not cursor:
                break

    def most_recent_orders(self, location_ids, limit=10):
        """Diagnostic: the single most recent orders, straight from Square --
        no date window, no state filter, no pagination loop. Bypasses every
        assumption in search_orders() above, to sanity-check against it."""
        body = {
            "location_ids": list(location_ids),
            "query": {"sort": {"sort_field": "CREATED_AT", "sort_order": "DESC"}},
            "limit": limit,
        }
        data = self._request("POST", "/v2/orders/search", json_body=body)
        return data.get("orders", [])

    # ------------------------------------------------------------------
    # Payments (transaction history)
    # ------------------------------------------------------------------
    def list_payments(self, begin_time=None, end_time=None, location_id=None):
        cursor = None
        while True:
            params = {"limit": 100, "sort_order": "ASC"}
            if begin_time:
                params["begin_time"] = begin_time
            if end_time:
                params["end_time"] = end_time
            if location_id:
                params["location_id"] = location_id
            if cursor:
                params["cursor"] = cursor
            data = self._request("GET", "/v2/payments", params=params)
            for payment in data.get("payments", []):
                yield payment
            cursor = data.get("cursor")
            if not cursor:
                break

    def most_recent_payments(self, limit=10):
        """Diagnostic: the single most recent payments, straight from Square --
        no date window, no pagination loop. Bypasses every assumption in
        list_payments() above, to sanity-check against it."""
        params = {"limit": limit, "sort_order": "DESC"}
        data = self._request("GET", "/v2/payments", params=params)
        return data.get("payments", [])

    # ------------------------------------------------------------------
    # Bookings (appointment history)
    # ------------------------------------------------------------------
    def list_bookings(self, location_id=None, start_at_min=None, start_at_max=None):
        cursor = None
        while True:
            params = {"limit": 200}
            if location_id:
                params["location_id"] = location_id
            if start_at_min:
                params["start_at_min"] = start_at_min
            if start_at_max:
                params["start_at_max"] = start_at_max
            if cursor:
                params["cursor"] = cursor
            data = self._request("GET", "/v2/bookings", params=params)
            for booking in data.get("bookings", []):
                yield booking
            cursor = data.get("cursor")
            if not cursor:
                break

    # ------------------------------------------------------------------
    # Customers (for repeat-client analysis)
    # ------------------------------------------------------------------
    def list_customers(self):
        cursor = None
        while True:
            params = {"limit": 100}
            if cursor:
                params["cursor"] = cursor
            data = self._request("GET", "/v2/customers", params=params)
            for customer in data.get("customers", []):
                yield customer
            cursor = data.get("cursor")
            if not cursor:
                break


def _extract_error_detail(resp):
    try:
        payload = resp.json()
        errors = payload.get("errors")
        if errors:
            return "; ".join(
                f"{e.get('category', '')}/{e.get('code', '')}: {e.get('detail', '')}"
                for e in errors
            )
        return str(payload)
    except ValueError:
        return resp.text[:500]

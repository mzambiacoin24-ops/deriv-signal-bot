import os
import requests


# ============================================================
# DERIV AUTHENTICATION
# ============================================================
#
# Kazi ya file hii:
# - Kusoma Deriv App ID kutoka ENV
# - Kusoma Deriv Personal Access Token kutoka ENV
# - Kuongea na authenticated Deriv REST API
# - Kupata account information
# - Kupata authenticated WebSocket URL kupitia OTP
#
# MUHIMU:
# HATUWEKI TOKEN HAPA MOJA KWA MOJA.
# Token itawekwa Railway ENV baadaye.
# ============================================================


API_BASE_URL = "https://api.derivws.com"


class DerivAuth:

    def __init__(self):

        self.app_id = os.getenv("DERIV_APP_ID")
        self.auth_token = os.getenv("DERIV_AUTH_TOKEN")

        if not self.app_id:
            raise RuntimeError(
                "DERIV_APP_ID haijawekwa kwenye environment variables."
            )

        if not self.auth_token:
            raise RuntimeError(
                "DERIV_AUTH_TOKEN haijawekwa kwenye environment variables."
            )

    # ========================================================
    # COMMON HEADERS
    # ========================================================

    def _headers(self):

        return {
            "Deriv-App-ID": self.app_id,
            "Authorization": f"Bearer {self.auth_token}",
            "Content-Type": "application/json"
        }

    # ========================================================
    # GENERIC GET
    # ========================================================

    def _get(self, endpoint):

        url = API_BASE_URL + endpoint

        response = requests.get(
            url,
            headers=self._headers(),
            timeout=20
        )

        if response.status_code >= 400:

            raise RuntimeError(
                f"Deriv authentication request failed "
                f"({response.status_code}): "
                f"{response.text}"
            )

        return response.json()

    # ========================================================
    # GET ALL ACCOUNTS
    # ========================================================

    def get_accounts(self):

        return self._get(
            "/trading/v1/options/accounts"
        )

    # ========================================================
    # GET ACCOUNT IDs
    # ========================================================

    def get_account_ids(self):

        response = self.get_accounts()

        data = response.get("data", [])

        if isinstance(data, dict):

            data = [data]

        account_ids = []

        for account in data:

            account_id = account.get("account_id")

            if account_id:
                account_ids.append(account_id)

        return account_ids

    # ========================================================
    # FIND DEMO ACCOUNT
    # ========================================================

    def get_demo_account(self):

        response = self.get_accounts()

        data = response.get("data", [])

        if isinstance(data, dict):

            data = [data]

        for account in data:

            if account.get("account_type") == "demo":

                return account

        return None

    # ========================================================
    # FIND REAL ACCOUNT
    # ========================================================

    def get_real_account(self):

        response = self.get_accounts()

        data = response.get("data", [])

        if isinstance(data, dict):

            data = [data]

        for account in data:

            if account.get("account_type") == "real":

                return account

        return None

    # ========================================================
    # GET AUTHENTICATED WEBSOCKET URL
    # ========================================================

    def get_websocket_url(
        self,
        account_id
    ):

        endpoint = (
            f"/trading/v1/options/accounts/"
            f"{account_id}/otp"
        )

        url = API_BASE_URL + endpoint

        response = requests.post(
            url,
            headers=self._headers(),
            timeout=20
        )

        if response.status_code >= 400:

            raise RuntimeError(
                f"Failed to obtain Deriv WebSocket OTP "
                f"({response.status_code}): "
                f"{response.text}"
            )

        data = response.json()

        websocket_url = (
            data.get("data", {})
                .get("url")
        )

        if not websocket_url:

            raise RuntimeError(
                "Deriv haikurudisha authenticated "
                "WebSocket URL."
            )

        return websocket_url


# ============================================================
# SIMPLE HELPERS
# ============================================================

def get_accounts():

    client = DerivAuth()

    return client.get_accounts()


def get_account_ids():

    client = DerivAuth()

    return client.get_account_ids()


def get_demo_account():

    client = DerivAuth()

    return client.get_demo_account()


def get_real_account():

    client = DerivAuth()

    return client.get_real_account()


def get_authenticated_websocket_url(
    account_id
):

    client = DerivAuth()

    return client.get_websocket_url(
        account_id
    )


# ============================================================
# LOCAL TEST
# ============================================================

if __name__ == "__main__":

    print(
        "========================================"
    )

    print(
        "DERIV AUTHENTICATION TEST"
    )

    print(
        "========================================"
    )

    try:

        client = DerivAuth()

        print(
            "\nAuthentication credentials found."
        )

        print(
            "App ID: configured"
        )

        print(
            "Token: configured"
        )

        print(
            "\nGetting Deriv accounts..."
        )

        accounts = client.get_accounts()

        print(
            "\nAccounts response received."
        )

        data = accounts.get(
            "data",
            []
        )

        if isinstance(data, dict):

            data = [data]

        for account in data:

            print(
                f"- Account: "
                f"{account.get('account_id')}"
            )

            print(
                f"  Type: "
                f"{account.get('account_type')}"
            )

            print(
                f"  Currency: "
                f"{account.get('currency')}"
            )

            print(
                f"  Status: "
                f"{account.get('status')}"
            )

        print(
            "\nDERIV AUTH: OK"
        )

    except Exception as e:

        print(
            "\nDERIV AUTH: FAILED"
        )

        print(
            str(e)
      )

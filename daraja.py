import base64
import os
import re
from datetime import datetime
from pathlib import Path

import requests
from requests.exceptions import Timeout, ConnectionError


BASE_DIR = Path(__file__).resolve().parent
SANDBOX_URL = "https://sandbox.safaricom.co.ke"
PRODUCTION_URL = "https://api.safaricom.co.ke"


def load_env_file(path=BASE_DIR / ".env"):
    """Load simple KEY=VALUE lines from a local .env file."""
    if not path.exists():
        return

    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env_file()

# Environment variables
ENVIRONMENT = os.getenv("DARAJA_ENVIRONMENT", "sandbox").strip().lower()
CONSUMER_KEY = os.getenv("DARAJA_CONSUMER_KEY")
CONSUMER_SECRET = os.getenv("DARAJA_CONSUMER_SECRET")
SHORTCODE = os.getenv("DARAJA_BUSINESS_SHORTCODE") or os.getenv("DARAJA_SHORTCODE")
PASSKEY = os.getenv("DARAJA_PASSKEY")
CALLBACK_URL = os.getenv("DARAJA_CALLBACK_URL")  # Must be HTTPS
BASE_URL = PRODUCTION_URL if ENVIRONMENT == "production" else SANDBOX_URL

# Cache token to avoid repeated auth calls
_cached_token = None
_token_expiry = None


class DarajaConfigError(ValueError):
    pass


def validate_config():
    required = {
        "DARAJA_CONSUMER_KEY": CONSUMER_KEY,
        "DARAJA_CONSUMER_SECRET": CONSUMER_SECRET,
        "DARAJA_BUSINESS_SHORTCODE": SHORTCODE,
        "DARAJA_PASSKEY": PASSKEY,
        "DARAJA_CALLBACK_URL": CALLBACK_URL,
    }
    missing = [key for key, value in required.items() if not value]
    if missing:
        raise DarajaConfigError(f"Missing required setting(s): {', '.join(missing)}")

    if not SHORTCODE.isdigit():
        raise DarajaConfigError("DARAJA_BUSINESS_SHORTCODE must contain digits only.")

    if not CALLBACK_URL.startswith("https://"):
        raise DarajaConfigError("DARAJA_CALLBACK_URL must be a public HTTPS URL.")

    if CALLBACK_URL.startswith("https://example.com") or "your-public-domain.example" in CALLBACK_URL:
        raise DarajaConfigError("Replace DARAJA_CALLBACK_URL with your real public HTTPS callback URL.")


def format_phone_number(phone_number):
    phone_number = re.sub(r"\D", "", str(phone_number or "").strip())

    if phone_number.startswith("01") or phone_number.startswith("07"):
        phone_number = "254" + phone_number[1:]
    elif phone_number.startswith("1") or phone_number.startswith("7"):
        phone_number = "254" + phone_number

    if (phone_number.startswith("2541") or phone_number.startswith("2547")) and len(phone_number) == 12:
        return phone_number

    raise ValueError("Phone number must be a valid Kenyan Safaricom number, for example 0712345678.")


def format_amount(amount):
    try:
        amount = int(amount)
    except (TypeError, ValueError):
        raise ValueError("Amount must be a whole number.") from None

    if amount < 1:
        raise ValueError("Amount must be at least 1.")

    return amount


def daraja_error_message(response):
    try:
        return response.json()
    except ValueError:
        return response.text


def get_access_token():
    """Get OAuth 2.0 access token (valid 1 hour)."""
    global _cached_token, _token_expiry

    validate_config()

    now = datetime.now().timestamp()
    if _cached_token and _token_expiry and now < _token_expiry:
        return _cached_token

    auth_url = f"{BASE_URL}/oauth/v1/generate?grant_type=client_credentials"

    try:
        response = requests.get(
            auth_url,
            auth=(CONSUMER_KEY, CONSUMER_SECRET),
            timeout=30,
        )
        response.raise_for_status()
        token_data = response.json()
        _cached_token = token_data.get("access_token")
        if not _cached_token:
            raise Exception(f"Daraja access token was not returned: {token_data}")
        _token_expiry = now + (int(token_data.get("expires_in", 3600)) - 60)
        return _cached_token
    except (Timeout, ConnectionError) as e:
        raise Exception(f"Auth timeout/connection error: {e}")
    except requests.exceptions.HTTPError as e:
        if response.status_code == 401:
            raise Exception("Invalid consumer key/secret")
        raise Exception(f"Auth failed: {response.status_code} - {daraja_error_message(response)}") from e


def initiate_stk_push(phone_number, amount, account_reference="Payment", transaction_desc="Daraja payment"):
    """
    Initiate STK Push (Lipa Na M-PESA Online).

    Args:
        phone_number: Customer phone, for example 0712345678 or 254712345678
        amount: Amount in KES (integer)
        account_reference: Unique identifier for the transaction
        transaction_desc: Description of the transaction

    Returns:
        dict with CheckoutRequestID and ResponseCode
    """
    phone_number = format_phone_number(phone_number)
    amount = format_amount(amount)
    account_reference = str(account_reference or "Payment")[:12]
    transaction_desc = str(transaction_desc or "Daraja payment")[:100]

    # Get access token
    access_token = get_access_token()

    # Generate timestamp and password
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    password_string = f"{SHORTCODE}{PASSKEY}{timestamp}"
    password = base64.b64encode(password_string.encode()).decode()

    # Prepare payload
    payload = {
        "BusinessShortCode": SHORTCODE,
        "Password": password,
        "Timestamp": timestamp,
        "TransactionType": "CustomerPayBillOnline",
        "Amount": amount,
        "PartyA": phone_number,
        "PartyB": SHORTCODE,
        "PhoneNumber": phone_number,
        "CallBackURL": CALLBACK_URL,
        "AccountReference": account_reference,
        "TransactionDesc": transaction_desc
    }
    
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }

    stk_url = f"{BASE_URL}/mpesa/stkpush/v1/processrequest"
    
    try:
        response = requests.post(
            stk_url,
            json=payload,
            headers=headers,
            timeout=30,
        )
        response.raise_for_status()
        result = response.json()

        if result.get("ResponseCode") == "0":
            return {
                "success": True,
                "CheckoutRequestID": result.get("CheckoutRequestID"),
                "ResponseCode": result.get("ResponseCode"),
                "ResponseDescription": result.get("ResponseDescription")
            }
        else:
            return {
                "success": False,
                "ResponseCode": result.get("ResponseCode"),
                "ResponseDescription": result.get("ResponseDescription")
            }
    except requests.exceptions.HTTPError as e:
        if response.status_code == 400:
            raise Exception(f"Bad request: {daraja_error_message(response)}") from e
        elif response.status_code == 401:
            raise Exception("Unauthorized: Invalid token") from e
        elif response.status_code == 403:
            raise Exception("Forbidden: Check credentials") from e
        else:
            raise Exception(f"STK Push failed: {response.status_code} - {daraja_error_message(response)}") from e
    except (Timeout, ConnectionError) as e:
        raise Exception(f"Request timeout/connection error: {e}")

# Example usage
if __name__ == "__main__":
    try:
        result = initiate_stk_push(
            phone_number="254111354842",  # Sandbox test number
            amount=1,
            account_reference="ORD-12345",
            transaction_desc="Payment for Order 12345"
        )
        print(result)
    except Exception as e:
        print(f"Error: {e}")

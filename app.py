import os
import json
import base64
import time
import secrets
import hashlib
import requests
import re
from datetime import datetime, timedelta
from flask import Flask, redirect, request, render_template, flash, session
from dotenv import load_dotenv
from urllib.parse import urlencode, urlparse, parse_qs
from flask import jsonify
from get_description import generate_post_text_for_asin, generate_tweet_from_prompt

load_dotenv()

CLIENT_ID = os.getenv("X_CLIENT_ID")
CLIENT_SECRET = os.getenv("X_CLIENT_SECRET")
CALLBACK_URL = os.getenv("CALLBACK_URL")
SCOPES = ["tweet.read", "tweet.write", "users.read", "offline.access"]

TOKEN_FILE = "users.json"  # Local token file
TWEETS_LOG_FILE = "tweets_log.json"  # File to log successful tweets

AUTH_URL = "https://twitter.com/i/oauth2/authorize"
TOKEN_URL = "https://api.twitter.com/2/oauth2/token"
ME_URL = "https://api.twitter.com/2/users/me"
TWEET_URL = "https://api.twitter.com/2/tweets"

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET", "dev-secret-change-me")

API_KEY = os.getenv("API_KEY", "azxtwbkey")  # API key for authentication

# ---------------- ASIN EXTRACTION ---------------- 
def extract_asin_from_url(amazon_url: str) -> str:
    """
    Extracts ASIN from various Amazon URL formats.
    Examples:
    - https://www.amazon.com/dp/B0863DW238
    - https://www.amazon.com/product-name/dp/B0863DW238/ref=...
    - https://www.amazon.com/gp/product/B0863DW238
    """
    if not amazon_url:
        raise ValueError("Amazon URL is required")
    
    amazon_url = amazon_url.strip()
    
    # Pattern 1: /dp/ASIN or /product/ASIN
    patterns = [
        r'/dp/([A-Z0-9]{10})',  # /dp/B0863DW238
        r'/gp/product/([A-Z0-9]{10})',  # /gp/product/B0863DW238
        r'/product/([A-Z0-9]{10})',  # /product/B0863DW238
        r'/dp/([A-Z0-9]{10})/',  # /dp/B0863DW238/
    ]
    
    for pattern in patterns:
        match = re.search(pattern, amazon_url)
        if match:
            asin = match.group(1)
            if len(asin) == 10:
                return asin
    
    # Pattern 2: Check query parameters
    parsed = urlparse(amazon_url)
    query_params = parse_qs(parsed.query)
    
    # Some URLs have ASIN in query params
    if 'asin' in query_params:
        asin = query_params['asin'][0]
        if len(asin) == 10:
            return asin
    
    raise ValueError(f"Could not extract ASIN from URL: {amazon_url}")

# ---------------- FIND USER BY EMAIL ---------------- 
def find_user_by_email(email: str):
    """Find user ID by email address."""
    email = email.strip().lower()
    for user_id, user_data in USERS.items():
        user_email = (user_data.get("email") or "").strip().lower()
        if user_email == email:
            return user_id, user_data
    return None, None

# ---------------- API KEY AUTHENTICATION ---------------- 
def check_api_key():
    """Check if API key is valid."""
    api_key = request.headers.get("X-API-Key")
    if not api_key or api_key != API_KEY:
        return False
    return True

@app.route("/generate_tweet", methods=["POST"])
def generate_tweet():
    data = request.get_json(silent=True) or {}
    asin = (data.get("asin") or "").strip()
    if not asin:
        return jsonify({"ok": False, "error": "ASIN is required"}), 400

    try:
        post_text = generate_post_text_for_asin(asin)
        return jsonify({"ok": True, "post_text": post_text})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400

@app.route("/api/generate-text", methods=["POST"])
def generate_text():
    """
    Endpoint for UI to generate tweet text from prompt.
    Expects JSON: {"prompt": "...", "amazon_url": "...", "asin": "..."} (all optional but at least one required)
    Returns: {"success": bool, "text": "...", "error": "..."}
    """
    data = request.get_json(silent=True) or {}
    prompt = (data.get("prompt") or "").strip()
    amazon_url = (data.get("amazon_url") or "").strip()
    asin = (data.get("asin") or "").strip()
    user_provided_url = (data.get("user_provided_url") or "").strip()
    
    if not prompt and not amazon_url and not asin:
        return jsonify({
            "success": False,
            "error": "Either prompt, amazon_url, or asin is required"
        }), 400
    
    try:
        # Use user_provided_url if explicitly provided, otherwise use amazon_url if it's a URL
        final_user_url = user_provided_url if user_provided_url else (amazon_url if amazon_url.startswith('http') else None)
        
        tweet_text = generate_tweet_from_prompt(
            prompt if prompt else "",
            amazon_url if amazon_url and amazon_url.startswith('http') else None,
            asin if asin else None,
            user_provided_url=final_user_url
        )
        return jsonify({
            "success": True,
            "text": tweet_text
        }), 200
    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500

@app.route("/api/post-amazon", methods=["POST"])
def post_amazon_product():
    """
    Endpoint for automatic Amazon product posting.
    Expects JSON: {"email": "...", "amazon_url": "..."}
    Returns: {"success": bool, "message": "...", "tweet_id": "...", "post_text": "..."}
    """
    # Get request data early for logging
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip()
    amazon_url = (data.get("amazon_url") or "").strip()
    
    # Check API key
    if not check_api_key():
        # Log failed API key attempt
        asin_attempt = None
        try:
            if amazon_url:
                asin_attempt = extract_asin_from_url(amazon_url)
        except:
            pass
        log_tweet_attempt(
            user_id=None,
            success=False,
            asin=asin_attempt,
            error_message="Invalid or missing API key",
            endpoint="/api/post-amazon"
        )
        return jsonify({
            "success": False,
            "message": "Invalid or missing API key"
        }), 401
    
    # Validate input
    if not email:
        log_tweet_attempt(
            user_id=None,
            success=False,
            error_message="Email is required",
            endpoint="/api/post-amazon"
        )
        return jsonify({
            "success": False,
            "message": "Email is required"
        }), 400
    
    if not amazon_url:
        log_tweet_attempt(
            user_id=None,
            success=False,
            error_message="Amazon URL is required",
            endpoint="/api/post-amazon"
        )
        return jsonify({
            "success": False,
            "message": "Amazon URL is required"
        }), 400
    
    try:
        # Extract ASIN from URL
        asin = extract_asin_from_url(amazon_url)
        
        # Find user by email
        user_id, user_data = find_user_by_email(email)
        if not user_id:
            # Log user not found error
            log_tweet_attempt(
                user_id=None,
                success=False,
                asin=asin,
                error_message=f"User with email '{email}' not found. Please login first.",
                endpoint="/api/post-amazon"
            )
            return jsonify({
                "success": False,
                "message": f"User with email '{email}' not found. Please login first."
            }), 404
        
        # Generate tweet content using AI (use the provided URL from user)
        post_text = generate_post_text_for_asin(asin, user_provided_url=amazon_url)
        
        # Post tweet with ASIN for logging
        success, message, tweet_id = post_tweet_v2(user_id, post_text, asin=asin)
        
        if success:
            return jsonify({
                "success": True,
                "message": "Tweet posted successfully",
                "tweet_id": tweet_id,
                "post_text": post_text,
                "asin": asin,
                "email": email,
                "username": user_data.get("username", "unknown")
            }), 200
        else:
            return jsonify({
                "success": False,
                "message": f"Failed to post tweet: {message}",
                "asin": asin,
                "email": email,
                "post_text": post_text
            }), 500
            
    except ValueError as e:
        # Log validation errors
        log_tweet_attempt(
            user_id=None,
            success=False,
            asin=None,
            error_message=f"Validation error: {str(e)}",
            endpoint="/api/post-amazon"
        )
        return jsonify({
            "success": False,
            "message": str(e)
        }), 400
    except Exception as e:
        # Log unexpected errors
        log_tweet_attempt(
            user_id=None,
            success=False,
            asin=None,
            error_message=f"Unexpected error: {str(e)}",
            endpoint="/api/post-amazon"
        )
        return jsonify({
            "success": False,
            "message": f"Error: {str(e)}"
        }), 500

# ------------------ USERS ------------------
if os.path.exists(TOKEN_FILE):
    with open(TOKEN_FILE, "r", encoding="utf-8") as f:
        USERS = json.load(f)
else:
    USERS = {}

def save_users():
    with open(TOKEN_FILE, "w", encoding="utf-8") as f:
        json.dump(USERS, f, ensure_ascii=False, indent=4)

# ---------------- TWEETS LOG ---------------- 
def load_tweets_log():
    """Load tweets log from file."""
    if os.path.exists(TWEETS_LOG_FILE):
        try:
            with open(TWEETS_LOG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            return []
    return []

def save_tweets_log(tweets_log):
    """Save tweets log to file."""
    with open(TWEETS_LOG_FILE, "w", encoding="utf-8") as f:
        json.dump(tweets_log, f, ensure_ascii=False, indent=2)

def log_tweet_attempt(user_id=None, success=True, tweet_id=None, asin=None, error_message=None, endpoint=None):
    """Log a tweet attempt (successful or failed)."""
    tweets_log = load_tweets_log()
    current_time = time.time()
    log_entry = {
        "timestamp": current_time,
        "date": datetime.fromtimestamp(current_time).strftime("%Y-%m-%d %H:%M:%S"),
        "success": success,
        "user_id": user_id,
        "tweet_id": tweet_id
    }
    # Add ASIN if provided
    if asin:
        log_entry["asin"] = asin
    # Add endpoint if provided
    if endpoint:
        log_entry["endpoint"] = endpoint
    # Add error message if failed
    if not success and error_message:
        log_entry["error"] = error_message
    
    tweets_log.append(log_entry)
    # Keep only last 1000 entries to prevent file from growing too large
    if len(tweets_log) > 1000:
        tweets_log = tweets_log[-1000:]
    save_tweets_log(tweets_log)

def log_successful_tweet(user_id, tweet_id=None, asin=None):
    """Log a successful tweet post (backward compatibility)."""
    log_tweet_attempt(user_id, success=True, tweet_id=tweet_id, asin=asin)

def get_successful_tweets_last_hour():
    """Get count of successful tweets in the last hour."""
    tweets_log = load_tweets_log()
    one_hour_ago = time.time() - 3600  # 1 hour in seconds
    # Count only successful tweets (backward compatible: if 'success' field doesn't exist, assume success)
    count = sum(1 for tweet in tweets_log 
                if tweet.get("timestamp", 0) >= one_hour_ago 
                and tweet.get("success", True))  # Default to True for backward compatibility
    return count

# ---------------- PKCE HELPERS ----------------
def make_pkce_pair():
    """
    Returns (verifier, challenge) for S256 PKCE.
    """
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("utf-8")
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("utf-8")
    return verifier, challenge

def basic_auth_header(client_id: str, client_secret: str) -> str:
    token = base64.b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode("utf-8")
    return f"Basic {token}"

# ---------------- OAUTH FLOW ----------------
@app.route("/login")
def login():
    if not CLIENT_ID or not CLIENT_SECRET or not CALLBACK_URL:
        flash("Missing env vars: X_CLIENT_ID / X_CLIENT_SECRET / CALLBACK_URL", "error")
        return redirect("/")

    verifier, challenge = make_pkce_pair()
    session["pkce_verifier"] = verifier
    session["oauth_state"] = secrets.token_urlsafe(16)

    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": CALLBACK_URL,
        "scope": " ".join(SCOPES),
        "state": session["oauth_state"],
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    url = f"{AUTH_URL}?{urlencode(params)}"
    return redirect(url)

@app.route("/callback")
def callback():
    code = request.args.get("code")
    state = request.args.get("state")

    if not code:
        flash("Authorization failed: missing code", "error")
        return redirect("/")

    if not state or state != session.get("oauth_state"):
        flash("Authorization failed: state mismatch", "error")
        return redirect("/")

    verifier = session.get("pkce_verifier")
    if not verifier:
        flash("Authorization failed: missing PKCE verifier", "error")
        return redirect("/")

    # Exchange code -> access token
    data = {
        "code": code,
        "grant_type": "authorization_code",
        "client_id": CLIENT_ID,
        "redirect_uri": CALLBACK_URL,
        "code_verifier": verifier,
    }

    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Authorization": basic_auth_header(CLIENT_ID, CLIENT_SECRET),
    }

    resp = requests.post(TOKEN_URL, data=data, headers=headers, timeout=30)
    if resp.status_code != 200:
        flash(f"Error getting token: {resp.status_code} {resp.text}", "error")
        return redirect("/")

    token_data = resp.json()
    access_token = token_data.get("access_token")
    refresh_token = token_data.get("refresh_token")
    expires_in = token_data.get("expires_in", 0)

    if not access_token:
        flash("Token response missing access_token", "error")
        return redirect("/")

    # Get user info
    headers_user = {"Authorization": f"Bearer {access_token}"}
    user_resp = requests.get(ME_URL, headers=headers_user, timeout=30)
    if user_resp.status_code != 200:
        flash(f"Failed to get user info: {user_resp.status_code} {user_resp.text}", "error")
        return redirect("/")

    user_info = user_resp.json()["data"]

    # Store user data temporarily in session for email collection
    session["pending_user"] = {
        "id": user_info["id"],
        "username": user_info.get("username") or user_info.get("name") or user_info["id"],
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_in": expires_in,
        "obtained_at": int(time.time()),
    }

    # Redirect to email entry page
    return redirect("/enter_email")

# ---------------- TOKEN REFRESH ----------------
def refresh_token_if_needed(user_id: str):
    """
    Refresh access token if expired (or close to expiry).
    Updates USERS + users.json.
    """
    user = USERS.get(user_id)
    if not user:
        raise RuntimeError("Unknown user")

    access_token = user.get("access_token")
    refresh_token = user.get("refresh_token")
    expires_in = int(user.get("expires_in") or 0)
    obtained_at = int(user.get("obtained_at") or 0)

    # If no expiry provided, assume token is valid (some apps may omit expires_in)
    if access_token and expires_in and obtained_at:
        # refresh 60 seconds early
        if time.time() < obtained_at + expires_in - 60:
            return  # still valid

    if not refresh_token:
        # Can't refresh; user must login again
        raise RuntimeError("No refresh_token. Please /login again and approve offline.access scope.")

    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": CLIENT_ID,
    }
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Authorization": basic_auth_header(CLIENT_ID, CLIENT_SECRET),
    }

    resp = requests.post(TOKEN_URL, data=data, headers=headers, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(f"Refresh failed: {resp.status_code} {resp.text}")

    new_data = resp.json()
    user["access_token"] = new_data.get("access_token", user["access_token"])
    user["expires_in"] = new_data.get("expires_in", user.get("expires_in", 0))
    user["obtained_at"] = int(time.time())

    # refresh token may rotate
    if new_data.get("refresh_token"):
        user["refresh_token"] = new_data["refresh_token"]

    USERS[user_id] = user
    save_users()

# ---------------- TWEET ----------------
def post_tweet_v2(user_id, text, asin=None):
    user = USERS.get(user_id)
    if not user:
        error_msg = "User not found"
        log_tweet_attempt(user_id, success=False, asin=asin, error_message=error_msg)
        return False, error_msg, None

    try:
        refresh_token_if_needed(user_id)
    except Exception as e:
        error_msg = f"Token refresh error: {e}"
        log_tweet_attempt(user_id, success=False, asin=asin, error_message=error_msg)
        return False, error_msg, None

    headers = {
        "Authorization": f"Bearer {USERS[user_id]['access_token']}",
        "Content-Type": "application/json",
    }
    payload = {"text": text}

    try:
        resp = requests.post(TWEET_URL, headers=headers, json=payload, timeout=30)
    except Exception as e:
        error_msg = f"Request error: {str(e)}"
        log_tweet_attempt(user_id, success=False, asin=asin, error_message=error_msg)
        return False, error_msg, None

    # Some clients return 201, some return 200
    if resp.status_code in (200, 201):
        try:
            response_data = resp.json()
            tweet_id = response_data.get("data", {}).get("id")
            # Log successful tweet with ASIN if provided
            log_tweet_attempt(user_id, success=True, tweet_id=tweet_id, asin=asin)
            return True, "Tweet posted", tweet_id
        except:
            # Log successful tweet even if we can't parse tweet_id
            log_tweet_attempt(user_id, success=True, tweet_id=None, asin=asin)
            return True, "Tweet posted", None
    else:
        # Log failed tweet attempt
        error_msg = f"{resp.status_code} {resp.text}"
        log_tweet_attempt(user_id, success=False, asin=asin, error_message=error_msg)
        return False, error_msg, None

# ---------------- API ENDPOINTS ---------------- 
@app.route("/api/post-tweet", methods=["POST"])
def api_post_tweet():
    """API endpoint for posting tweets via AJAX."""
    data = request.get_json(silent=True) or {}
    user_id = data.get("account", "").strip()
    text = (data.get("text") or "").strip()

    if not user_id or not text:
        return jsonify({
            "success": False,
            "message": "Please select account & enter text"
        }), 400

    ok, msg, tweet_id = post_tweet_v2(user_id, text)
    
    # Get updated stats
    successful_tweets_last_hour = get_successful_tweets_last_hour()
    
    return jsonify({
        "success": ok,
        "message": msg,
        "tweet_id": tweet_id,
        "successful_tweets_last_hour": successful_tweets_last_hour
    }), 200 if ok else 500

@app.route("/api/stats", methods=["GET"])
def api_stats():
    """API endpoint to get current statistics."""
    successful_tweets_last_hour = get_successful_tweets_last_hour()
    total_accounts = len(USERS)
    bot_status = "Active" if total_accounts > 0 else "Inactive"
    
    return jsonify({
        "successful_tweets_last_hour": successful_tweets_last_hour,
        "total_accounts": total_accounts,
        "bot_status": bot_status
    }), 200

# ---------------- UI ---------------- 
@app.route("/", methods=["GET", "POST"])
def index():
    accounts = [{"id": uid, "name": u.get("username", uid)} for uid, u in USERS.items()]
    
    # Statistics
    total_accounts = len(USERS)
    successful_tweets_last_hour = get_successful_tweets_last_hour()
    bot_status = "Active" if total_accounts > 0 else "Inactive"

    if request.method == "POST":
        # Check if it's an AJAX request
        if request.headers.get("Content-Type") == "application/json":
            return api_post_tweet()
        
        # Regular form submission (fallback)
        user_id = request.form.get("account")
        text = (request.form.get("text") or "").strip()

        if not user_id or not text:
            flash("Please select account & enter text", "error")
        else:
            ok, msg, _ = post_tweet_v2(user_id, text)
            flash(msg, "success" if ok else "error")

    return render_template("index.html", 
                         accounts=accounts,
                         total_accounts=total_accounts,
                         successful_tweets_last_hour=successful_tweets_last_hour,
                         bot_status=bot_status)

# ---------------- EMAIL ENTRY ---------------- 
@app.route("/enter_email", methods=["GET", "POST"])
def enter_email():
    # Check if there's pending user data in session
    pending_user = session.get("pending_user")
    if not pending_user:
        flash("Invalid request", "error")
        return redirect("/")

    if request.method == "POST":
        email = (request.form.get("email") or "").strip()
        
        if not email:
            flash("Please enter email address", "error")
            return render_template("enter_email.html", username=pending_user.get("username"))
        
        # Basic email validation
        if "@" not in email or "." not in email.split("@")[1]:
            flash("Invalid email address", "error")
            return render_template("enter_email.html", username=pending_user.get("username"))

        # Save user with email
        user_id = pending_user["id"]
        USERS[user_id] = {
            "username": pending_user["username"],
            "email": email,
            "access_token": pending_user["access_token"],
            "refresh_token": pending_user["refresh_token"],
            "expires_in": pending_user["expires_in"],
            "obtained_at": pending_user["obtained_at"],
        }
        save_users()

        # Clear session data
        session.pop("pending_user", None)

        flash(f"Account added: {pending_user['username']} ({email})", "success")
        return redirect("/")

    # GET request - show email entry form
    return render_template("enter_email.html", username=pending_user.get("username"))

if __name__ == "__main__":
    # Server configuration - accepts requests from all network interfaces
    # For production, consider using gunicorn: gunicorn -w 4 -b 0.0.0.0:5000 app:app
    host = os.getenv("FLASK_HOST", "0.0.0.0")  # Default: accept from all interfaces
    port = int(os.getenv("FLASK_PORT", 5000))  # Default port: 5000
    debug = os.getenv("FLASK_DEBUG", "False").lower() == "true"  # Default: False for production
    
    print(f"🚀 Server starting on http://{host}:{port}")
    print(f"📡 Accepting requests from all network interfaces")
    print(f"🔧 Debug mode: {debug}")
    
    app.run(debug=debug, host=host, port=port)

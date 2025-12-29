import os
import json
import base64
import time
import secrets
import hashlib
import requests
from flask import Flask, redirect, request, render_template, flash, session
from dotenv import load_dotenv
from urllib.parse import urlencode
from flask import jsonify
from get_description import generate_post_text_for_asin


load_dotenv()

CLIENT_ID = os.getenv("X_CLIENT_ID")
CLIENT_SECRET = os.getenv("X_CLIENT_SECRET")
CALLBACK_URL = os.getenv("CALLBACK_URL")
SCOPES = ["tweet.read", "tweet.write", "users.read", "offline.access"]

TOKEN_FILE = "users.json"  # Lokal token dosyası

AUTH_URL = "https://twitter.com/i/oauth2/authorize"
TOKEN_URL = "https://api.twitter.com/2/oauth2/token"
ME_URL = "https://api.twitter.com/2/users/me"
TWEET_URL = "https://api.twitter.com/2/tweets"

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET", "dev-secret-change-me")

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

# ------------------ USERS ------------------
if os.path.exists(TOKEN_FILE):
    with open(TOKEN_FILE, "r", encoding="utf-8") as f:
        USERS = json.load(f)
else:
    USERS = {}

def save_users():
    with open(TOKEN_FILE, "w", encoding="utf-8") as f:
        json.dump(USERS, f, ensure_ascii=False, indent=4)

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
        flash(f"Hata token alırken: {resp.status_code} {resp.text}", "error")
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
        flash(f"Kullanıcı bilgisi alınamadı: {user_resp.status_code} {user_resp.text}", "error")
        return redirect("/")

    user_info = user_resp.json()["data"]

    # Save in USERS + file
    USERS[user_info["id"]] = {
        "username": user_info.get("username") or user_info.get("name") or user_info["id"],
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_in": expires_in,
        "obtained_at": int(time.time()),
    }
    save_users()

    flash(f"Hesap eklendi: {USERS[user_info['id']]['username']}", "success")
    return redirect("/")

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
def post_tweet_v2(user_id, text):
    user = USERS.get(user_id)
    if not user:
        return False, "User not found"

    try:
        refresh_token_if_needed(user_id)
    except Exception as e:
        return False, f"Token refresh error: {e}"

    headers = {
        "Authorization": f"Bearer {USERS[user_id]['access_token']}",
        "Content-Type": "application/json",
    }
    payload = {"text": text}

    resp = requests.post(TWEET_URL, headers=headers, json=payload, timeout=30)

    # Some clients return 201, some return 200
    if resp.status_code in (200, 201):
        return True, "Tweet gönderildi"
    return False, f"{resp.status_code} {resp.text}"

# ---------------- UI ----------------
@app.route("/", methods=["GET", "POST"])
def index():
    accounts = [{"id": uid, "name": u.get("username", uid)} for uid, u in USERS.items()]

    if request.method == "POST":
        user_id = request.form.get("account")
        text = (request.form.get("text") or "").strip()

        if not user_id or not text:
            flash("Lütfen hesap ve metin girin / Please select account & enter text", "error")
        else:
            ok, msg = post_tweet_v2(user_id, text)
            flash(msg, "success" if ok else "error")

    return render_template("index.html", accounts=accounts)

if __name__ == "__main__":
    app.run(debug=True)

import os
import json
import time
import hashlib
import hmac
import datetime
import logging
from typing import Dict, Any, List

import requests
from dotenv import load_dotenv
from openai import AzureOpenAI

load_dotenv()

logger = logging.getLogger("get_description")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)


# =========================================================
# Azure OpenAI Configuration
# =========================================================
class AzureOpenAIConfig:
    def __init__(self):
        self.endpoint = os.getenv("AZURE_OPENAI_ENDPOINT", "https://ai-services-az-1.openai.azure.com/")
        self.api_key = os.getenv("AZURE_OPENAI_API_KEY")
        self.deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4.1")
        self.api_version = os.getenv("AZURE_OPENAI_API_VERSION", "2024-12-01-preview")

        if not self.api_key:
            raise ValueError("AZURE_OPENAI_API_KEY not found in environment variables")

        self.client = AzureOpenAI(
            api_version=self.api_version,
            azure_endpoint=self.endpoint,
            api_key=self.api_key,
        )


_azure_config = None


def get_azure_client() -> AzureOpenAIConfig:
    global _azure_config
    if _azure_config is None:
        logger.info("Initializing Azure OpenAI client...")
        _azure_config = AzureOpenAIConfig()
    return _azure_config


# =========================================================
# JSON Schema (Structured Output)
# =========================================================
TWEET_SCHEMA = {
    "type": "object",
    "properties": {
        "description": {"type": "string", "description": "Promotional tweet description, maximum 25 words"},
        "hashtag1": {"type": "string", "description": "First broad category hashtag. Must start with #, be lowercase single word."},
        "hashtag2": {"type": "string", "description": "Second broad category hashtag, different from hashtag1. Must start with #, be lowercase single word."},
    },
    "required": ["description", "hashtag1", "hashtag2"],
    "additionalProperties": False,
}


# =========================================================
# Amazon PA-API Helper (Title + Features + Affiliate URL)
# =========================================================
class AmazonApiHelper:
    def __init__(
        self,
        access_key: str,
        secret_key: str,
        associate_tag: str,
        region: str = "us-east-1",
        endpoint: str = "webservices.amazon.com",
        marketplace: str = "www.amazon.com",
    ):
        self.access_key = access_key
        self.secret_key = secret_key
        self.associate_tag = associate_tag
        self.region = region
        self.endpoint = endpoint
        self.marketplace = marketplace
        self.service = "ProductAdvertisingAPI"

    def _hmac_sha256(self, key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

    def _sign_auth_header(self, amz_date: str, datestamp: str, request_payload: str) -> str:
        algorithm = "AWS4-HMAC-SHA256"
        method = "POST"
        canonical_uri = "/paapi5/getitems"
        canonical_querystring = ""
        canonical_headers = (
            f"content-encoding:amz-1.0\n"
            f"host:{self.endpoint}\n"
            f"x-amz-date:{amz_date}\n"
        )
        signed_headers = "content-encoding;host;x-amz-date"

        payload_hash = hashlib.sha256(request_payload.encode("utf-8")).hexdigest()
        canonical_request = (
            f"{method}\n{canonical_uri}\n{canonical_querystring}\n"
            f"{canonical_headers}\n{signed_headers}\n{payload_hash}"
        )

        credential_scope = f"{datestamp}/{self.region}/{self.service}/aws4_request"
        string_to_sign = (
            f"{algorithm}\n{amz_date}\n{credential_scope}\n"
            f"{hashlib.sha256(canonical_request.encode('utf-8')).hexdigest()}"
        )

        k_date = self._hmac_sha256(("AWS4" + self.secret_key).encode("utf-8"), datestamp)
        k_region = self._hmac_sha256(k_date, self.region)
        k_service = self._hmac_sha256(k_region, self.service)
        k_signing = self._hmac_sha256(k_service, "aws4_request")

        signature = hmac.new(k_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

        return (
            f"{algorithm} Credential={self.access_key}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )

    def get_item_info(self, asin: str) -> Dict[str, Any]:
        """
        Returns:
            {
              "asin": "...",
              "url": "affiliate DetailPageURL",
              "title": "...",
              "features": ["...", ...]
            }
        """
        if not self.access_key or not self.secret_key or not self.associate_tag:
            raise ValueError("Amazon API credentials missing. Set AMAZON_ACCESS_KEY / AMAZON_SECRET_KEY / AMAZON_ASSOC_TAG")

        request_payload = json.dumps({
            "ItemIds": [asin],
            "Resources": [
                "ItemInfo.Title",
                "ItemInfo.Features",
            ],
            "PartnerTag": self.associate_tag,
            "PartnerType": "Associates",
            "Marketplace": self.marketplace,
        })

        t = datetime.datetime.utcnow()
        amz_date = t.strftime("%Y%m%dT%H%M%SZ")
        datestamp = t.strftime("%Y%m%d")

        headers = {
            "Content-Encoding": "amz-1.0",
            "Content-Type": "application/json; charset=utf-8",
            "Host": self.endpoint,
            "X-Amz-Date": amz_date,
            "X-Amz-Target": "com.amazon.paapi5.v1.ProductAdvertisingAPIv1.GetItems",
        }
        headers["Authorization"] = self._sign_auth_header(amz_date, datestamp, request_payload)

        url = f"https://{self.endpoint}/paapi5/getitems"
        resp = requests.post(url, headers=headers, data=request_payload, timeout=20)

        if resp.status_code != 200:
            raise RuntimeError(f"Amazon API error {resp.status_code}: {resp.text}")

        data = resp.json()
        items = (data.get("ItemsResult") or {}).get("Items") or []
        if not items:
            raise RuntimeError(f"No item returned for ASIN {asin}")

        item = items[0]
        detail_url = item.get("DetailPageURL") or ""

        title = (((item.get("ItemInfo") or {}).get("Title") or {}).get("DisplayValue")) or ""
        features = (((item.get("ItemInfo") or {}).get("Features") or {}).get("DisplayValues")) or []

        return {"asin": asin, "url": detail_url, "title": title, "features": features}


# =========================================================
# Azure OpenAI: Generate Description + 2 Hashtags
# =========================================================
def generate_tweet_content(title: str, bullets: List[str], max_retries: int = 3) -> Dict[str, Any]:
    config = get_azure_client()

    product_info = f"Product: {title}\n\nKey Features:\n"
    product_info += "\n".join([f"- {b}" for b in bullets[:6] if b])

    CATEGORY_HINTS = {
        "dvd": "tech",
        "disc": "office",
        "camera": "tech",
        "microphone": "tech",
        "keyboard": "tech",
        "mouse": "tech",
        "monitor": "tech",
        "lamp": "home",
        "pillow": "home",
        "shirt": "fashion",
        "toy": "kids",
        "pet": "pet",
        "garden": "garden",
        "fitness": "fitness",
        "supplement": "wellness",
        "bag": "travel",
    }

    category_hint = ""
    tl = (title or "").lower()
    for word, cat in CATEGORY_HINTS.items():
        if word in tl:
            category_hint = cat
            break

    system_prompt = """You write short, persuasive promotional tweets for X (Twitter).

STRICT OUTPUT RULES:
- Return JSON only, matching the provided schema.
- description: maximum 25 words, benefit-focused, salesy, human tone, no brand names, no product codes, no ASIN.
- hashtags: exactly 2, lowercase, one word each, must start with #, broad category/lifestyle tags (e.g. #tech #office #home #travel #fitness #pet #garden).
- hashtags must be different.
"""

    user_prompt = f"""{product_info}

Generate tweet content with EXACTLY 2 category hashtags."""
    if category_hint:
        user_prompt += f"\nTry to align hashtags with theme: {category_hint}"

    for attempt in range(1, max_retries + 1):
        try:
            resp = config.client.chat.completions.create(
                model=config.deployment,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "tweet_content",
                        "strict": True,
                        "schema": TWEET_SCHEMA,
                    },
                },
                temperature=0.7,
                top_p=0.9,
                max_completion_tokens=250,
            )

            raw = (resp.choices[0].message.content or "").strip()
            data = json.loads(raw)

            desc = (data.get("description") or "").strip()
            hashtag1 = (data.get("hashtag1") or "").strip().lower()
            hashtag2 = (data.get("hashtag2") or "").strip().lower()

            if not desc or not hashtag1 or not hashtag2:
                raise ValueError("Incomplete AI response")

            if not hashtag1.startswith("#"):
                hashtag1 = f"#{hashtag1}"
            if not hashtag2.startswith("#"):
                hashtag2 = f"#{hashtag2}"
            if hashtag1 == hashtag2:
                hashtag2 = "#lifestyle"

            # Enforce 25 words max
            words = desc.split()
            if len(words) > 25:
                desc = " ".join(words[:25])

            return {"description": desc, "hashtags": [hashtag1, hashtag2]}

        except Exception as e:
            logger.warning(f"Azure OpenAI attempt {attempt} failed: {e}")
            time.sleep(2 ** attempt)

    # fallback
    return {
        "description": "Discover a must-have upgrade that makes everyday life easier—bring it home today!",
        "hashtags": ["#home", "#lifestyle"],
    }


# =========================================================
# Public function you will call from main.py
# =========================================================
def generate_post_text_for_asin(asin: str) -> str:
    """
    Returns the final tweet text string:

    <description>
    <affiliate_url>
    #amazon <tag1> <tag2>
    """
    asin = (asin or "").strip()
    if not asin:
        raise ValueError("ASIN is required")

    access = os.getenv("AMAZON_ACCESS_KEY")
    secret = os.getenv("AMAZON_SECRET_KEY")
    tag = os.getenv("AMAZON_ASSOC_TAG")

    if not (access and secret and tag):
        raise RuntimeError("Missing Amazon PA-API env vars: AMAZON_ACCESS_KEY / AMAZON_SECRET_KEY / AMAZON_ASSOC_TAG")

    amazon = AmazonApiHelper(access_key=access, secret_key=secret, associate_tag=tag)

    item = amazon.get_item_info(asin)
    title = item.get("title") or ""
    features = item.get("features") or []
    affiliate_url = item.get("url") or f"https://www.amazon.com/dp/{asin}"

    if not title:
        raise RuntimeError(f"PA-API returned empty title for ASIN {asin}")

    ai = generate_tweet_content(title, features)

    # 3 tags: #amazon + 2 ai tags
    tags = ["#amazon"] + ai["hashtags"]
    tags_str = " ".join(tags)

    post_text = f"{ai['description']}\n{affiliate_url}\n{tags_str}\n"
    return post_text


# =========================================================
# Local test
# =========================================================
if __name__ == "__main__":
    asin_number = "B00KALEHJE"
    print(generate_post_text_for_asin(asin_number))

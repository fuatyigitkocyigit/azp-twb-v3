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
# Generate Tweet from Prompt (for UI)
# =========================================================
def generate_tweet_from_prompt(prompt: str, amazon_url: str = None, asin: str = None, user_provided_url: str = None) -> str:
    """
    Generate a tweet from a user prompt, optionally with Amazon product info.
    If amazon_url or asin is provided, gets product info and combines with prompt.
    Otherwise, generates a simple tweet from the prompt.
    
    Args:
        prompt: User's text prompt
        amazon_url: Amazon URL (for extracting ASIN)
        asin: Direct ASIN number
        user_provided_url: User's provided URL to use in tweet (if provided, this will be used instead of affiliate URL)
    
    Returns tweet text (max 280 chars) with hashtags.
    """
    import re
    from urllib.parse import urlparse, parse_qs
    
    # Extract ASIN from URL if provided
    extracted_asin = None
    if amazon_url:
        try:
            asin_pattern = r'/dp/([A-Z0-9]{10})|/gp/product/([A-Z0-9]{10})|/product/([A-Z0-9]{10})'
            match = re.search(asin_pattern, amazon_url)
            if match:
                extracted_asin = match.group(1) or match.group(2) or match.group(3)
            
            if not extracted_asin:
                parsed = urlparse(amazon_url)
                query_params = parse_qs(parsed.query)
                if 'asin' in query_params:
                    extracted_asin = query_params['asin'][0]
        except Exception as e:
            logger.warning(f"Failed to extract ASIN from URL: {e}")
    
    # Use provided ASIN or extracted ASIN
    final_asin = asin or extracted_asin
    
    # If we have ASIN, get product info and enhance with prompt
    if final_asin and len(final_asin) == 10:
        try:
            access = os.getenv("AMAZON_ACCESS_KEY")
            secret = os.getenv("AMAZON_SECRET_KEY")
            tag = os.getenv("AMAZON_ASSOC_TAG")
            
            if access and secret and tag:
                amazon = AmazonApiHelper(access_key=access, secret_key=secret, associate_tag=tag)
                item = amazon.get_item_info(final_asin)
                title = item.get("title") or ""
                features = item.get("features") or []
                # Use user provided URL if available, otherwise create simple ASIN-only link
                if user_provided_url:
                    final_url = user_provided_url
                else:
                    # Simple link with only ASIN, no affiliate tags or parameters
                    final_url = f"https://www.amazon.com/dp/{final_asin}"
                
                if title:
                    config = get_azure_client()
                    
                    # Always create enhanced, longer tweet when ASIN is provided
                    # Use prompt if available, otherwise create detailed product tweet
                    if prompt:
                        user_context = f"\n\nUser's emphasis/context: {prompt}"
                    else:
                        user_context = ""
                    
                    # Create detailed product description
                    product_info = f"Product: {title}\n\nKey Features:\n"
                    product_info += "\n".join([f"- {f}" for f in features[:6] if f])
                    product_info += user_context
                    
                    # Enhanced system prompt for longer, more engaging tweets
                    system_prompt = """You write engaging, persuasive promotional tweets for X (Twitter).

STRICT OUTPUT RULES:
- Return JSON only, matching the provided schema.
- description: 20-25 words (not too short!), benefit-focused, salesy, human tone, engaging.
- Focus on benefits, value, and why someone should care.
- NO brand names, product codes, ASIN numbers, or model numbers in description.
- hashtags: exactly 2, lowercase, one word each, must start with #, broad category/lifestyle tags.
- hashtags must be different.
"""
                    
                    user_prompt = f"""{product_info}

Create an engaging, detailed promotional tweet (20-25 words) that highlights the product benefits and value.{user_context if prompt else ""}"""
                    
                    # Category hints
                    CATEGORY_HINTS = {
                        "dvd": "tech", "disc": "office", "camera": "tech", "microphone": "tech",
                        "keyboard": "tech", "mouse": "tech", "monitor": "tech", "lamp": "home",
                        "pillow": "home", "shirt": "fashion", "toy": "kids", "pet": "pet",
                        "garden": "garden", "fitness": "fitness", "supplement": "wellness", "bag": "travel",
                    }
                    
                    category_hint = ""
                    tl = (title or "").lower()
                    for word, cat in CATEGORY_HINTS.items():
                        if word in tl:
                            category_hint = cat
                            break
                    
                    if category_hint:
                        user_prompt += f"\nTry to align hashtags with theme: {category_hint}"
                    
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
                        
                        # Ensure 20-25 words (not too short!)
                        words = desc.split()
                        if len(words) < 15:
                            # If too short, try to expand - but this shouldn't happen with the new prompt
                            pass  # Keep as is, the prompt should ensure 20-25 words
                        elif len(words) > 25:
                            desc = " ".join(words[:25])
                        
                        tags = ["#amazon", hashtag1, hashtag2]
                        tags_str = " ".join(tags)
                        
                        # Build tweet and ensure it's under 280 characters
                        tweet_text = f"{desc}\n{final_url}\n{tags_str}"
                        
                        # If over 280, truncate description
                        if len(tweet_text) > 280:
                            url_and_tags_len = len(f"\n{final_url}\n{tags_str}")
                            max_desc_len = 280 - url_and_tags_len
                            if max_desc_len > 0:
                                words = desc.split()
                                truncated_desc = ""
                                for word in words:
                                    if len(truncated_desc + " " + word) <= max_desc_len:
                                        truncated_desc += (" " if truncated_desc else "") + word
                                    else:
                                        break
                                if truncated_desc:
                                    desc = truncated_desc
                                else:
                                    # If even one word doesn't fit, use first few chars
                                    desc = desc[:max_desc_len].rsplit(' ', 1)[0] if ' ' in desc[:max_desc_len] else desc[:max_desc_len-3] + "..."
                            tweet_text = f"{desc}\n{final_url}\n{tags_str}"
                        
                        return tweet_text
                    except Exception as e:
                        logger.warning(f"Enhanced tweet generation failed: {e}")
                        # Fall back to standard generation
                        return generate_post_text_for_asin(final_asin)
        except Exception as e:
            logger.warning(f"Failed to get Amazon product info: {e}")
            # Fall through to prompt-based generation
    
    # Generate simple tweet from prompt
    config = get_azure_client()
    
    system_prompt = """You write engaging, concise tweets for X (Twitter).

STRICT OUTPUT RULES:
- Return JSON only, matching the provided schema.
- description: maximum 25 words, engaging, natural tone, relevant to the prompt.
- hashtags: exactly 2, lowercase, one word each, must start with #, relevant to the content.
- hashtags must be different.
- Do NOT include product codes, ASIN numbers, or model numbers.
"""
    
    user_prompt = f"""Create an engaging tweet based on this prompt: {prompt}

Generate tweet content with EXACTLY 2 relevant hashtags."""
    
    for attempt in range(1, 4):
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
            
            # Combine description and hashtags
            tweet_text = f"{desc}\n{hashtag1} {hashtag2}"
            
            # Ensure total length is under 280
            if len(tweet_text) > 280:
                max_desc_len = 280 - len(f"\n{hashtag1} {hashtag2}")
                if max_desc_len > 0:
                    desc = desc[:max_desc_len].rsplit(' ', 1)[0]
                    tweet_text = f"{desc}\n{hashtag1} {hashtag2}"
            
            return tweet_text
            
        except Exception as e:
            logger.warning(f"Azure OpenAI attempt {attempt} failed: {e}")
            if attempt < 3:
                time.sleep(2 ** attempt)
    
    # fallback
    return f"{prompt[:200]}\n#lifestyle #inspiration"


# =========================================================
# Public function you will call from main.py
# =========================================================
def generate_post_text_for_asin(asin: str, user_provided_url: str = None) -> str:
    """
    Returns the final tweet text string:

    <description>
    <affiliate_url or user_provided_url>
    #amazon <tag1> <tag2>
    
    Args:
        asin: Amazon ASIN number
        user_provided_url: User's provided URL to use instead of affiliate URL
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
    # Use user provided URL if available, otherwise create simple ASIN-only link
    if user_provided_url:
        final_url = user_provided_url
    else:
        # Simple link with only ASIN, no affiliate tags or parameters
        final_url = f"https://www.amazon.com/dp/{asin}"

    if not title:
        raise RuntimeError(f"PA-API returned empty title for ASIN {asin}")

    ai = generate_tweet_content(title, features)

    # 3 tags: #amazon + 2 ai tags
    tags = ["#amazon"] + ai["hashtags"]
    tags_str = " ".join(tags)

    # Build tweet and ensure it's under 280 characters
    post_text = f"{ai['description']}\n{final_url}\n{tags_str}"
    
    # If over 280, truncate description
    if len(post_text) > 280:
        url_and_tags_len = len(f"\n{final_url}\n{tags_str}")
        max_desc_len = 280 - url_and_tags_len
        if max_desc_len > 0:
            words = ai['description'].split()
            truncated_desc = ""
            for word in words:
                if len(truncated_desc + " " + word) <= max_desc_len:
                    truncated_desc += (" " if truncated_desc else "") + word
                else:
                    break
            if truncated_desc:
                ai['description'] = truncated_desc
            else:
                # If even one word doesn't fit, use first few chars
                ai['description'] = ai['description'][:max_desc_len-3] + "..."
        post_text = f"{ai['description']}\n{final_url}\n{tags_str}"
    
    return post_text


# =========================================================
# Local test
# =========================================================
if __name__ == "__main__":
    asin_number = "B00KALEHJE"
    print(generate_post_text_for_asin(asin_number))

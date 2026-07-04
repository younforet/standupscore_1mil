import os
import sys
import logging
import requests
from googleapiclient.discovery import build
import urllib.parse
from urllib.parse import urlparse, parse_qs
from dotenv import load_dotenv
import uuid

# Load environment variables from .env file if present
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)

# Environment variables (GitHub Secrets)
EMAIL = os.environ.get("STANDUPSCORE_EMAIL")
PASSWORD = os.environ.get("STANDUPSCORE_PASSWORD")
YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY")
COLLECTION_ID = os.environ.get("COLLECTION_ID")

# API Endpoints
BASE_URL = "https://standupscore.com"
STANDUPS_API_URL = f"{BASE_URL}/api/v2/standups/"
COLLECTION_API_URL = f"{BASE_URL}/api/v1/private/collections/{COLLECTION_ID}"

# Logto configuration
LOGTO_CLIENT_ID = "9b9twlljym0k171eb0nq6"
LOGTO_BASE_URL = "https://logto.standupscore.com"


def extract_youtube_id(url):
    """Extract the YouTube video ID from a URL."""
    if not url:
        return None
    # Handle youtu.be format
    if "youtu.be" in url:
        return urlparse(url).path.strip("/")
    # Handle youtube.com format
    if "youtube.com" in url:
        query = parse_qs(urlparse(url).query)
        if "v" in query:
            return query["v"][0]
    return None


def login(session):
    """Log into Standupscore using Logto OIDC flow."""
    logging.info("Logging into Standupscore via Logto...")
    
    # 1. Start OIDC interaction
    state = str(uuid.uuid4())
    redirect_uri = f"{BASE_URL}/callback"
    auth_url = f"{LOGTO_BASE_URL}/oidc/auth?response_type=code&scope=openid%20email&client_id={LOGTO_CLIENT_ID}&redirect_uri={urllib.parse.quote(redirect_uri)}&resource={BASE_URL}&state={state}"
    
    try:
        session.get(auth_url)
        
        # 2. Identify the user
        session.put(f"{LOGTO_BASE_URL}/api/experience", json={"interactionEvent": "SignIn", "identifier": EMAIL})
        
        # 3. Verify password
        pwd_res = session.post(f"{LOGTO_BASE_URL}/api/experience/verification/password", json={"identifier": {"type": "email", "value": EMAIL}, "password": PASSWORD})
        verification_id = pwd_res.json().get("verificationId")
        
        # 4. Identification
        session.post(f"{LOGTO_BASE_URL}/api/experience/identification", json={"verificationId": verification_id})
        
        # 5. Submit
        session.post(f"{LOGTO_BASE_URL}/api/experience/submit", json={})
        
        latest_interaction_token = session.cookies.get('_interaction')
        if not latest_interaction_token:
            logging.error("Failed to get Logto interaction token from cookies.")
            return False
            
        res = session.get(f"{LOGTO_BASE_URL}/oidc/auth/{latest_interaction_token}", allow_redirects=False)
        location = res.headers.get("Location", "")
        
        if not location or "code=" not in location:
            # Sometimes it redirects to /consent first
            if location and "/consent" in location:
                logging.info("Redirected to /consent. Attempting to auto-accept consent...")
                
                # We found the exact endpoint using our brute-force script!
                consent_res = session.post(f"{LOGTO_BASE_URL}/api/interaction/consent", json={"action": "accept"})
                if consent_res.status_code >= 400:
                    logging.error(f"Failed to auto-accept consent. Status: {consent_res.status_code}, Body: {consent_res.text}")
                    return False
                    
                redirect_to = consent_res.json().get("redirectTo")
                if not redirect_to:
                    logging.error("Consent response did not contain 'redirectTo' URL.")
                    return False
                    
                # Fetch the redirected URL which should finally give us the callback URL with the code
                callback_res = session.get(redirect_to, allow_redirects=False)
                location = callback_res.headers.get("Location", "")
                
        if not location or "code=" not in location:
            logging.error(f"Could not extract OIDC code from redirect. Status: {res.status_code}, Location: {location}, Headers: {dict(res.headers)}")
            return False
            
        # Parse the code from the redirect URL
        parsed_url = urlparse(location)
        query_params = parse_qs(parsed_url.query)
        code = query_params.get("code", [None])[0]
        
        if not code:
            logging.error("OIDC code not found in URL parameters.")
            return False
            
        # 6. Exchange the code for a Standupscore session
        token_res = session.post("https://standupscore.com/api/auth/token", json={"code": code})
        token_res.raise_for_status()
        
        access_token = token_res.json().get("access_token")
        if access_token:
            session.headers.update({"Authorization": f"Bearer {access_token}"})
        
        logging.info("Successfully logged in and received Standupscore session.")
        return True
    except Exception as e:
        logging.error(f"Logto Login failed: {e}")
        return False


def fetch_all_concerts(session, language="ru"):
    """Fetch all concerts from the Standupscore JSON API."""
    logging.info(f"Fetching all concerts from Standupscore API (language={language})...")
    concerts = []
    page = 1
    total_pages = 1

    while page <= total_pages:
        logging.info(f"Fetching page {page}/{total_pages}...")
        try:
            url = f"{STANDUPS_API_URL}?page={page}&limit=50"
            if language:
                url += f"&language={language}"
            res = session.get(url)
            res.raise_for_status()
            data = res.json()

            total_pages = data.get("total_pages", 1)
            items = data.get("standups", [])
            concerts.extend(items)
            page += 1
        except Exception as e:
            logging.error(f"Failed to fetch concerts page {page}: {e}")
            break

    logging.info(f"Found {len(concerts)} total concerts.")
    return concerts


def get_youtube_views(youtube_ids):
    """Fetch view counts for a list of YouTube IDs in batches of 50."""
    logging.info(f"Fetching view counts for {len(youtube_ids)} YouTube videos...")
    youtube = build(
        "youtube", "v3", developerKey=YOUTUBE_API_KEY, cache_discovery=False
    )
    views_map = {}

    # YouTube API limits 50 ids per request
    for i in range(0, len(youtube_ids), 50):
        batch = youtube_ids[i : i + 50]
        try:
            request = youtube.videos().list(part="statistics", id=",".join(batch))
            response = request.execute()
            for item in response.get("items", []):
                vid = item["id"]
                views = int(item["statistics"].get("viewCount", 0))
                views_map[vid] = views
        except Exception as e:
            logging.error(f"Failed to fetch YouTube batch: {e}")

    return views_map


def update_collection(session, slugs_to_add):
    """Update the collection with new standup slugs via internal API."""
    items = []
    for i, slug in enumerate(slugs_to_add, start=1):
        items.append({
            "entity_slug": slug,
            "entity_type": "standup",
            "place": i
        })
        
    logging.info(f"Replacing collection with {len(items)} concerts. Sending PUT request...")
    
    # For PUT requests, we need csrf token if present, though Bearer might be enough
    csrf_token = session.cookies.get("csrftoken") or session.cookies.get("XSRF-TOKEN")
    if csrf_token:
        session.headers.update({"X-CSRFToken": csrf_token})

    put_res = session.put(COLLECTION_API_URL, json={"items": items})
    if put_res.status_code in (200, 204):
         logging.info(f"Successfully updated collection with {len(items)} concerts.")
         return True
    else:
         logging.error(f"Failed to update collection. Status: {put_res.status_code}, Body: {put_res.text}")
         return False


def main():
    if not all([EMAIL, PASSWORD, YOUTUBE_API_KEY, COLLECTION_ID]):
        logging.error(
            "Missing required environment variables. Please set STANDUPSCORE_EMAIL, STANDUPSCORE_PASSWORD, YOUTUBE_API_KEY, and COLLECTION_ID."
        )
        sys.exit(1)

    session = requests.Session()
    # Add common user agent
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
        }
    )

    # Step 1: Login
    if not login(session):
        logging.error("Exiting due to login failure.")
        sys.exit(1)

    # Step 2: Fetch all concerts and extract YouTube IDs
    concerts = fetch_all_concerts(session)

    youtube_to_standup = {}
    for concert in concerts:
        video_sources = concert.get("video_sources", {})
        if not video_sources:
            continue

        yt_url = video_sources.get("YT")
        if yt_url:
            yt_id = extract_youtube_id(yt_url)
            if yt_id:
                youtube_to_standup[yt_id] = concert["slug"]

    # Step 3: Fetch view counts from YouTube
    yt_ids = list(youtube_to_standup.keys())
    if not yt_ids:
        logging.info("No YouTube links found in the catalog.")
        sys.exit(0)

    views_map = get_youtube_views(yt_ids)

    # Step 4: Identify > 5M views, sort by views, and update collection
    million_club = []
    for yt_id, views in views_map.items():
        if views >= 5_000_000:
            slug = youtube_to_standup[yt_id]
            million_club.append((slug, views))

    # Sort descending by view count
    million_club.sort(key=lambda x: x[1], reverse=True)
    million_club_slugs = [slug for slug, views in million_club]

    logging.info(f"Found {len(million_club_slugs)} concerts with > 5M views. Sorted by views.")

    update_collection(session, million_club_slugs)

    logging.info("Update complete!")


if __name__ == "__main__":
    main()

# hubspot.py

import asyncio
import base64
import json
import secrets

from fastapi.responses import HTMLResponse
from backend.integrations.integration_item import IntegrationItem
from backend.redis_client import add_key_value_redis, delete_key_redis, get_value_redis
from fastapi import HTTPException, Path, Request
import httpx
from dotenv import load_dotenv
import os

env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(dotenv_path=env_path)

CLIENT_ID = os.getenv("HUBSPOT_CLIENT_ID")
CLIENT_SECRET = os.getenv("HUBSPOT_CLIENT_SECRET")
SCOPE = os.getenv("HUBSPOT_SCOPE")
AUTH_BASE_URL = os.getenv("HUBSPOT_AUTH_BASE_URL")
REDIRECT_URI = os.getenv("HUBSPOT_REDIRECT_URI")


# Return the authorization URL for HubSpot to the frontend
async def authorize_hubspot(user_id, org_id):
    state_data = {
        "state": secrets.token_urlsafe(32),
        "user_id": user_id,
        "org_id": org_id,
    }

    # encode the state and store it in redis
    encoded_state = base64.urlsafe_b64encode(
        json.dumps(state_data).encode("utf-8")
    ).decode("utf-8")

    # Create authorization URL
    auth_url = f"{AUTH_BASE_URL}?client_id={CLIENT_ID}&redirect_uri={REDIRECT_URI}&scope={SCOPE}&state={encoded_state}"

    # Store the state in Redis
    await add_key_value_redis(
        f"hubspot_state:{org_id}:{user_id}", json.dumps(state_data), expire=600
    )

    return auth_url


# HubSpot redirects back to this endpoint after use authorizes the app
async def oauth2callback_hubspot(request: Request):
    if request.query_params.get("error"):
        raise HTTPException(
            status_code=400, detail=request.query_params.get("error_description")
        )

    # 1. Extract the authorization code and state from the request
    code = request.query_params.get("code")
    encoded_state = request.query_params.get("state")
    state_data = json.loads(base64.urlsafe_b64decode(encoded_state).decode("utf-8"))

    original_state = state_data.get("state")
    user_id = state_data.get("user_id")
    org_id = state_data.get("org_id")

    # 2. Retrieve the original state from Redis and compare
    saved_state = await get_value_redis(f"hubspot_state:{org_id}:{user_id}")
    if not saved_state or original_state != json.loads(saved_state).get("state"):
        raise HTTPException(status_code=400, detail="State does not match.")

    # 3. Exchange the authorization code for an access token
    token_url = "https://api.hubapi.com/oauth/v1/token"

    async with httpx.AsyncClient() as client:
        response = await asyncio.gather(
            client.post(
                "https://api.hubapi.com/oauth/v1/token",
                json={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": REDIRECT_URI,
                    "client_id": CLIENT_ID,
                    "client_secret": CLIENT_SECRET,
                },
                headers={
                    "Content-Type": "application/json",
                },
            ),
            delete_key_redis(f"hubspot_state:{org_id}:{user_id}"),
        )

    if response.status_code != 200:
        raise HTTPException(
            status_code=400, detail="Failed to exchange code for token."
        )

    # 4. Store the access token in Redis
    token_data = response.json()
    await add_key_value_redis(
        f"hubspot_credentials:{org_id}:{user_id}",
        json.dumps(token_data),
        expire=token_data.get("expires_in", 3600),
    )

    # 5. Return a simple HTML page that closes the window
    close_window_script = """
    <html>
        <script>
            window.close();
        </script>
    </html>
    """

    return HTMLResponse(content=close_window_script)


async def get_hubspot_credentials(user_id, org_id):
    # 1. Get stored credentials
    credentials = await get_value_redis(f"hubspot_credentials:{org_id}:{user_id}")

    if not credentials:
        raise HTTPException(status_code=400, detail="No credentials found.")

    credentials = json.loads(credentials)

    # 2. Delete credentials from Redis after retrieving
    await delete_key_redis(f"hubspot_credentials:{org_id}:{user_id}")

    # 3. Return credentials to the caller
    return credentials


async def create_integration_item_metadata_object(response_json_item, item_type):
    item_id = response_json_item.get("id")

    if item_type == "contact":
        item_name = (
            response_json_item.get("properties", {}).get("firstname", "")
            + " "
            + response_json_item.get("properties", {}).get("lastname", "")
        )
    else:
        item_name = response_json_item.get("properties", {}).get("name", "")

    integration_metadata = IntegrationItem(
        id=item_id,
        type=item_type,
        name=item_name,
    )

    return integration_metadata


async def get_items_hubspot(credentials):
    # Fetch user items from HubSpot using the stored credentials
    credentials = json.loads(credentials)
    access_token = credentials.get("access_token")

    hubspot_endpoints = [
        "https://api.hubapi.com/crm/v3/objects/contacts",
        "https://api.hubapi.com/crm/v3/objects/companies",
        "https://api.hubapi.com/crm/v3/objects/deals",
    ]

    item_types = ["contact", "company", "deal"]

    async with httpx.AsyncClient() as client:
        tasks = [
            client.get(endpoint, headers={"Authorization": f"Bearer {access_token}"})
            for endpoint in hubspot_endpoints
        ]  # A list of requests that haven't run yet

        # List of raw http responses
        responses = await asyncio.gather(*tasks)

    # List of python dicts containing the actual data of https responses
    responses_json = [
        response.json() for response in responses if response.status_code == 200
    ]

    list_of_integration_item_metadata = []
    for response_json, item_type in zip(responses_json, item_types):
        # Skip if the response is not successful
        if response_json.status_code != 200:
            continue

        # Create an IntegrationItem for each item in the response
        for item in response_json.get("results", []):
            item_metadata = await create_integration_item_metadata_object(
                item, item_type
            )
            list_of_integration_item_metadata.append(item_metadata)

    # Return the list of IntegrationItem objects to the caller
    return list_of_integration_item_metadata

import requests
import json

def call_models_api():
    url = "https://chat-api.tamu.ai/openai/models"
    headers = {
        "accept": "application/json",
        "Authorization": "Bearer sk-42a2be2a3f2c42d4a8c91ddba3d882ff"  # Replace with your API key
    }
    
    response = requests.get(url, headers=headers)
    response.raise_for_status()  # Raises an exception for bad status codes
    
    return response.json()

# Usage
try:
    result = call_models_api()
    print(json.dumps(result, indent=2))
except Exception as e:
    print(f"Error calling API: {e}")
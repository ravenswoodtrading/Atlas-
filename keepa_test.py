import os
import requests
from dotenv import load_dotenv

load_dotenv()

key = os.getenv("KEEPA_API_KEY")

response = requests.get(
    "https://api.keepa.com/token",
    params={"key": key},
    headers={
        "User-Agent": "Atlas/1.0",
        "Accept": "application/json"
    },
    timeout=30
)

print("Status:", response.status_code)
print("URL:", response.url)
print("Headers:", response.headers)
print("Body:", response.text)
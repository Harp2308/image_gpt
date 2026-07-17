import os
import requests
import msal
from dotenv import load_dotenv

load_dotenv()

T = os.getenv("AZURE_TENANT_ID")
C = os.getenv("AZURE_CLIENT_ID")
S = os.getenv("AZURE_CLIENT_SECRET")
HOST = os.getenv("SHAREPOINT_HOST")       # e.g. contoso.sharepoint.com
SITE = os.getenv("SHAREPOINT_SITE_PATH")  # e.g. /sites/colep

app = msal.ConfidentialClientApplication(
    C, authority=f"https://login.microsoftonline.com/{T}", client_credential=S
)
token = app.acquire_token_for_client(["https://graph.microsoft.com/.default"])
headers = {"Authorization": f"Bearer {token['access_token']}"}

# Step 1: Get site ID
# Step 1: Get site ID
site_resp = requests.get(
    f"https://graph.microsoft.com/v1.0/sites/{HOST}:{SITE}",
    headers=headers
)
site_resp.raise_for_status()
site_id = site_resp.json()["id"]

# Step 2: Find Documents drive
drives = requests.get(
    f"https://graph.microsoft.com/v1.0/sites/{site_id}/drives",
    headers=headers
).json()["value"]

doc_drive = next(d for d in drives if d["name"] == "Documents")
drive_id = doc_drive["id"]

# Step 3: List contents
def list_folder(path):
    items = requests.get(
        f"https://graph.microsoft.com/v1.0/drives/{drive_id}/root:{path}:/children",
        headers=headers
    ).json().get("value", [])
    
    for item in items:
        icon = "📁" if "folder" in item else "📄"
        indent = "  " * (path.count("/") )
        print(f"{indent}{icon} {item['name']}")
        if "folder" in item:
            list_folder(f"{path}/{item['name']}")

# list_folder("/ShopFloor")


import os

def download_file(item_id, save_path):
    url = f"https://graph.microsoft.com/v1.0/drives/{drive_id}/items/{item_id}/content"
    r = requests.get(url, headers=headers, stream=True)
    r.raise_for_status()
    with open(save_path, "wb") as f:
        for chunk in r.iter_content(chunk_size=8192):
            f.write(chunk)
    print(f"✅ {save_path}")

def download_from_folder(path, folder_name):
    items = requests.get(
        f"https://graph.microsoft.com/v1.0/drives/{drive_id}/root:{path}:/children",
        headers=headers
    ).json().get("value", [])

    files = [i for i in items if "file" in i][:4]  # first 4 files only

    if files:
        save_dir = f"documents/{folder_name}"
        os.makedirs(save_dir, exist_ok=True)

        for item in files:
            save_path = os.path.join(save_dir, item["name"])
            download_file(item["id"], save_path)

    # recurse into subfolders
    for item in items:
        if "folder" in item:
            download_from_folder(f"{path}/{item['name']}", item["name"])

download_from_folder("/ShopFloor", "ShopFloor")
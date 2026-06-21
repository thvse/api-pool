import urllib.request
import json
import time

BASE_URL = "http://localhost:5100"

def get(path):
    req = urllib.request.Request(f"{BASE_URL}{path}", method="GET")
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        return {"error": str(e)}

def post(path, data):
    req = urllib.request.Request(f"{BASE_URL}{path}", data=json.dumps(data).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        return {"error": str(e)}

def delete(path):
    req = urllib.request.Request(f"{BASE_URL}{path}", method="DELETE")
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        return {"error": str(e)}

print("Starting tests...")
time.sleep(2)

print("\n--- 1. Testing GET /api/endpoints ---")
print(get("/api/endpoints"))

print("\n--- 2. Testing ADD Endpoint ---")
new_ep = {
    "name": "test_ep_local",
    "base_url": "https://api.openai.com/v1",
    "api_key": "sk-dummy",
    "model": "gpt-3.5-turbo",
    "priority": 1,
    "enabled": True,
    "max_retries": 1,
    "timeout": 10,
    "use_proxy": False
}
print(post("/api/endpoints", new_ep))

print("\n--- 3. Testing GET /api/endpoints again ---")
eps = get("/api/endpoints")
# it might be a list or dict if it errored
if isinstance(eps, list):
    print([e.get("name") for e in eps if isinstance(e, dict)])
else:
    print(eps)

# find ID
test_id = None
if isinstance(eps, list):
    for e in eps:
        if isinstance(e, dict) and e.get("name") == "test_ep_local":
            test_id = e["id"]

print("\n--- 4. Testing POST /api/test (Test Pool logic) ---")
if test_id:
    print(post("/api/test", {"id": test_id, "message": "hello"}))

print("\n--- 5. Testing GET /api/token-stats ---")
print(get("/api/token-stats").keys() if isinstance(get("/api/token-stats"), dict) else get("/api/token-stats"))

print("\n--- 6. Testing GET /api/chat-logs ---")
print(get("/api/chat-logs"))

print("\n--- 7. Testing DELETE /api/logs ---")
print(delete("/api/logs"))

print("\n--- 8. Testing DELETE /api/token-stats ---")
print(delete("/api/token-stats"))

print("\n--- 9. Testing DELETE /api/chat-logs ---")
print(delete("/api/chat-logs"))

print("\n--- 10. Delete the test endpoint ---")
if test_id:
    print(delete(f"/api/endpoints/{test_id}"))

print("Tests completed!")

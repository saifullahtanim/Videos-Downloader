import requests, json

resp = requests.get("http://127.0.0.1:5000/download-status/b0fcaaae-a541-40a8-93fb-a8927b1c8469")
data = resp.json()
print("Response keys:", list(data.keys()))
print("Has job_id?", "job_id" in data)
print(f"total_bytes: {data.get('total_bytes')}")
print(f"speed: {data.get('speed')}")

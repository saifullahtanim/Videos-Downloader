import requests, time

url = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
resp = requests.post("http://127.0.0.1:5000/bulk-download", json={
    "items": [{"url": url, "title": "Test", "thumbnail": "", "resolution": "360", "format_type": "video"}]
})

data = resp.json()
if data.get("jobs"):
    job_id = data["jobs"][0]["job_id"]
    print(f"Job: {job_id}")
    for i in range(12):
        time.sleep(1)
        status = requests.get(f"http://127.0.0.1:5000/download-status/{job_id}").json()
        print(f"Poll {i+1}: status={status.get('status')}, total_bytes={status.get('total_bytes')}, speed={status.get('speed')}")
        if status.get("status") == "success":
            print(f"✅ Stats populated: total={status.get('total_bytes')}, speed={status.get('speed')}")
            break

import requests, time, json

print('Posting bulk-download...')
r=requests.post('http://127.0.0.1:5000/bulk-download', json={'items':[{'url':'https://www.youtube.com/watch?v=dQw4w9WgXcQ','resolution':'auto','format_type':'audio'}]})
print('bulk response', r.status_code)
try:
    print(r.json())
except Exception:
    print(r.text)

j=r.json()
jobs=j.get('jobs',[])
if not jobs:
    print('No jobs returned')
else:
    job=jobs[0].get('job_id')
    print('job id', job)
    for i in range(6):
        s=requests.get(f'http://127.0.0.1:5000/download-status/{job}')
        print(i, 'status', s.status_code)
        try:
            print(s.json())
        except Exception:
            print(s.text)
        time.sleep(1)
    print('Cancelling job')
    requests.post(f'http://127.0.0.1:5000/cancel-download/{job}')
    s=requests.get(f'http://127.0.0.1:5000/download-status/{job}')
    print('after cancel', s.status_code)
    try:
        print(s.json())
    except Exception:
        print(s.text)
